"""Independent ownership, inventory and recovery for the two private API targets."""

from __future__ import annotations

import time
from pathlib import Path

from botocore.exceptions import ClientError

from . import aws
from .store import event, events

SUITE = "lambda-vpc"


def is_lambda(proof):
    return proof.get("suite") == SUITE


def identity(reader, proof):
    aws.validate_proof(proof)
    if reader.call("sts", "get_caller_identity")["Account"] != proof["account"]:
        raise RuntimeError("AWS identity changed; refusing private API operation")


def expected_name(proof, number):
    if number not in (1, 2):
        raise RuntimeError("Unknown Lambda target number")
    return f"{proof['app']}-api{number}"


def before_create(root, sdk, proof, number, name):
    if name != expected_name(proof, number):
        raise RuntimeError("Unexpected private API namespace")
    if any(
        r["kind"] == "api_intent" and r["app"] == proof["app"] and r["number"] == number
        for r in events(root)
    ):
        raise RuntimeError("Private API creation was already attempted; do not repeat it")
    reader = aws.Reader(sdk)
    identity(reader, proof)
    if any(row.get("name") == name for row in reader.pages("apigateway", "get_rest_apis", "items")):
        raise RuntimeError(f"Private API namespace is not empty: {name}")
    event(
        root,
        "api_intent",
        **proof,
        number=number,
        name=name,
        tags={"stelvio:app": proof["app"], "stelvio:env": "proof"},
        namespace_empty=True,
    )


def _get(reader, identifier):
    try:
        return reader.call("apigateway", "get_rest_api", restApiId=identifier)
    except ClientError as error:
        if error.response["Error"]["Code"] != "NotFoundException":
            raise
        return None


def _owned(value, proof, number):
    return (
        value.get("name") == expected_name(proof, number)
        and value.get("tags", {}).get("stelvio:app") == proof["app"]
        and value.get("tags", {}).get("stelvio:env") == "proof"
        and value.get("endpointConfiguration", {}).get("types") == ["PRIVATE"]
    )


def api_inventory(root, reader, proof, numbers=(1, 2)):
    """Resolve each intent independently, including creation replies lost before receipt writing."""
    identity(reader, proof)
    records = [r for r in events(root) if r.get("app") == proof["app"]]
    listed = list(reader.pages("apigateway", "get_rest_apis", "items"))
    result = {}
    for number in numbers:
        name = expected_name(proof, number)
        intents = [r for r in records if r["kind"] == "api_intent" and r["number"] == number]
        completions = [
            r
            for r in records
            if r["kind"] in {"api_created", "api_discovered"} and r["number"] == number
        ]
        ids = {r["api_id"] for r in completions}
        matches = [row for row in listed if row.get("name") == name]
        ids.update(row["id"] for row in matches)
        if len(ids) > 1 or len(intents) > 1:
            raise RuntimeError(f"Ambiguous API ownership for {name}; retain evidence")
        intent = intents[0] if intents else None
        if intent and (
            intent["name"] != name
            or not intent.get("namespace_empty")
            or any(intent[k] != proof[k] for k in ("account", "region", "project"))
        ):
            raise RuntimeError(f"Private API ownership intent changed: {name}")
        identifier = next(iter(ids), None)
        current = _get(reader, identifier) if identifier else None
        if (
            intent
            and not identifier
            and not any(r["kind"] == "api_not_created" and r["number"] == number for r in records)
        ):
            raise RuntimeError(
                f"Unconfirmed API creation for {name}; an empty listing is not proof of absence"
            )
        if current is not None and (
            not intent or current.get("id") != identifier or not _owned(current, proof, number)
        ):
            raise RuntimeError(f"Foreign private API retained: {name}/{identifier}")
        if current is not None and not completions:
            event(root, "api_discovered", **proof, number=number, name=name, api_id=identifier)
        result[str(number)] = {
            "name": name,
            "api_id": identifier,
            "outcome": "remaining" if current is not None else "absent",
            "creation": "attempted" if intent else "not-started",
        }
    return result


def retry_throttling(reader, operation, **parameters):
    deadline = time.monotonic() + 90
    delay = 1
    for attempt in range(10):
        try:
            return reader.call("apigateway", operation, **parameters)
        except ClientError as error:
            if (
                error.response["Error"]["Code"] != "TooManyRequestsException"
                or attempt == 9
                or time.monotonic() + delay > deadline
            ):
                raise
            time.sleep(delay)
            delay = min(delay * 2, 10)
    raise AssertionError("Unreachable throttle retry")


def recover_apis(root, sdk, proof):
    actions = []
    for number in (1, 2):
        action = {"app": proof["app"], "api_number": number}
        try:
            # One API failure must not hide or prevent cleanup of the other API.
            reader = aws.Reader(sdk, seconds=240)
            view = api_inventory(root, reader, proof, numbers=(number,))[str(number)]
            identifier = view["api_id"]
            action["api_id"] = identifier
            if view["outcome"] == "remaining":
                current = _get(reader, identifier)
                if current is not None:
                    if not _owned(current, proof, number):
                        raise RuntimeError("API ownership changed before deletion")
                    event(
                        root,
                        "recovery_aws_call",
                        **proof,
                        service="apigateway",
                        operation="delete_rest_api",
                        arguments={"restApiId": identifier},
                    )
                    try:
                        retry_throttling(reader, "delete_rest_api", restApiId=identifier)
                    except Exception:
                        # Lost replies never trigger a second mutation. A confirmed
                        # absence may complete recovery; a surviving API retains the error.
                        if _get(reader, identifier) is not None:
                            raise
                    deadline = time.monotonic() + 120
                    while _get(reader, identifier) is not None:
                        if time.monotonic() >= deadline:
                            raise TimeoutError(f"Private API {identifier} still exists")
                        time.sleep(2)
            action["outcome"] = "completed"
        except Exception as error:
            action.update(outcome="failed", error=str(error))
        actions.append(action)
    return actions


def capture_proof(root: Path, sdk, proof, previous=()):
    """Keep the ordinary inventory untouched; add Lambda-specific resource views."""
    if not is_lambda(proof):
        return aws.capture(sdk, proof, previous)
    records = [r for r in events(root) if r.get("app") == proof["app"]]
    topology = next((r["targets"] for r in reversed(records) if r["kind"] == "lambda_topology"), {})
    known = {
        "endpoint_records": [
            dict(id=t["endpoint_id"], vpc=t["vpc_id"], interfaces=[])
            for t in topology.values()
            if t.get("endpoint_id")
        ]
    }
    snapshot = aws.capture(sdk, proof, [*previous, known])
    reader = aws.Reader(sdk)
    apis = {}
    for number in (1, 2):
        try:
            apis.update(api_inventory(root, reader, proof, numbers=(number,)))
        except Exception as error:
            apis[str(number)] = {
                "name": expected_name(proof, number),
                "api_id": None,
                "outcome": "unknown",
                "error": str(error),
            }
            snapshot.setdefault("unknown", []).append(f"API {number}: {error}")
    snapshot["apis"] = apis
    if remaining := [v["api_id"] for v in apis.values() if v["outcome"] == "remaining"]:
        snapshot["remaining"]["private_apis"] = remaining
    faults = {}
    for row in records:
        if row["kind"] in {"fault_enter", "fault_exit"}:
            faults[row["fault_id"]] = {
                k: row[k] for k in ("fault_id", "fault", "instance_id", "kind")
            }
    instances = {r["instance_id"] for r in faults.values()}
    instances.update(t["instance_id"] for t in topology.values())
    removed = set()
    for identifier in sorted(instances):
        try:
            reservations = reader.call("ec2", "describe_instances", InstanceIds=[identifier]).get(
                "Reservations", []
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "InvalidInstanceID.NotFound":
                raise
            removed.add(identifier)
            continue
        nodes = [(r.get("OwnerId"), node) for r in reservations for node in r.get("Instances", [])]
        if not nodes:
            removed.add(identifier)
        for account, node in nodes:
            tags = {t["Key"]: t["Value"] for t in node.get("Tags", [])}
            if (
                account != proof["account"]
                or node["InstanceId"] != identifier
                or any(
                    tags.get(k) != v
                    for k, v in {"stelvio:app": proof["app"], "stelvio:env": proof["env"]}.items()
                )
            ):
                raise RuntimeError("Proof bastion ownership changed during fault audit")
            if node["State"]["Name"] == "terminated":
                removed.add(identifier)
            else:
                values = snapshot["remaining"].setdefault("instances", [])
                if identifier not in values:
                    values.append(identifier)
    for fault in faults.values():
        fault["outcome"] = (
            "completed"
            if fault["kind"] == "fault_exit"
            else "interrupted-bastion-removed"
            if fault["instance_id"] in removed
            else "uncertain"
        )
        if fault["outcome"] == "uncertain":
            snapshot["remaining"].setdefault("faults", []).append(fault)
    snapshot["faults"] = list(faults.values())
    endpoints = {row["id"]: row for row in snapshot.get("endpoint_records", [])}
    snapshot["vpc_views"] = {
        str(number): {
            "api": apis[str(number)],
            "topology": topology.get(str(number), {}),
            "endpoint": endpoints.get(topology.get(str(number), {}).get("endpoint_id")),
            "faults": [
                fault
                for fault in faults.values()
                if fault["instance_id"] == topology.get(str(number), {}).get("instance_id")
            ],
        }
        for number in (1, 2)
    }
    return snapshot
