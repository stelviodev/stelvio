"""Observable CLI tunnel assertions; AWS clients share the integration provider."""

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request

from stelvio.tunnel.credentials import AWS_IO

from .assert_helpers import _boto3_session


def invoke_url(url: str) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310 - AWS-owned Function URL
            return {"status": response.status, "body": json.loads(response.read())}
    except urllib.error.HTTPError as error:
        return {"status": error.code, "body": error.read().decode()}


def assert_example_response(url: str, *, marker: str, pid: int) -> dict:
    response = invoke_url(url)
    assert response["status"] == 200
    payload = response["body"]
    assert {k: payload[k] for k in ("ok", "ping", "read_back", "host_matches")} == {
        "ok": True,
        "ping": True,
        "read_back": True,
        "host_matches": True,
    }
    assert payload["local_marker"] == marker
    assert payload["pid"] == pid
    assert payload["uid"] == os.geteuid()
    assert payload["uid"] != 0
    assert payload["replica_set"] == "rs0"
    assert payload["seen_hosts"]
    assert all(h.endswith(".docdb.amazonaws.com") for h in payload["seen_hosts"])
    for host in payload["seen_hosts"]:
        addresses = socket.getaddrinfo(host, 27017)
        assert addresses  # Actual OS resolution, including discovered member names.
    return payload


def owned_transport(instance: str, owner: str) -> dict:
    assert re.fullmatch(r"[0-9a-f]{16}", owner)
    ssm = _boto3_session().client("ssm", config=AWS_IO)
    pages = ssm.get_paginator("describe_sessions").paginate(
        State="Active",
        Filters=[{"key": "Target", "value": instance}],
    )
    owned = [
        s
        for p in pages
        for s in p.get("Sessions", [])
        if re.fullmatch(
            rf"stelvio/{owner}/[0-9a-f]{{32}}",
            s.get("Reason", ""),
        )
    ]
    assert len(owned) == 1
    return owned[0]


def interrupt_transport(instance: str, owner: str) -> str:
    session = owned_transport(instance, owner)
    _boto3_session().client("ssm", config=AWS_IO).terminate_session(SessionId=session["SessionId"])
    return session["SessionId"]


def wait_new_transport(instance: str, old: str, owner: str, *, timeout: int = 180) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            session = owned_transport(instance, owner)
            if session["SessionId"] != old:
                return session
        except AssertionError:
            pass
        time.sleep(1)
    raise TimeoutError(f"Transport did not reconnect within {timeout}s")


def assert_application_absent(
    app: str, env: str, *, include_lambda_logs: bool = True, sdk=None
) -> None:
    if sdk is None:
        sdk = _boto3_session()
    ec2 = sdk.client("ec2", config=AWS_IO)
    assert ec2.describe_vpcs(Filters=[{"Name": "tag:stelvio:app", "Values": [app]}])["Vpcs"] == []
    prefix = f"{app}-{env}-"
    docdb = sdk.client("docdb", config=AWS_IO)
    assert [
        c["DBClusterIdentifier"]
        for c in docdb.describe_db_clusters()["DBClusters"]
        if c["DBClusterIdentifier"].startswith(prefix)
    ] == []
    for operation, field, name in (
        ("describe_db_subnet_groups", "DBSubnetGroups", "DBSubnetGroupName"),
        (
            "describe_db_cluster_parameter_groups",
            "DBClusterParameterGroups",
            "DBClusterParameterGroupName",
        ),
    ):
        assert not [
            item[name]
            for page in docdb.get_paginator(operation).paginate()
            for item in page[field]
            if item[name].startswith(prefix)
        ]
    functions = [
        f["FunctionName"]
        for p in sdk.client("lambda", config=AWS_IO).get_paginator("list_functions").paginate()
        for f in p["Functions"]
        if f["FunctionName"].startswith(prefix)
    ]
    assert functions == []
    if include_lambda_logs:
        assert not [
            group["logGroupName"]
            for page in sdk.client("logs", config=AWS_IO)
            .get_paginator("describe_log_groups")
            .paginate(logGroupNamePrefix=f"/aws/lambda/{prefix}")
            for group in page.get("logGroups", [])
        ]
    assert (
        ec2.describe_security_groups(Filters=[{"Name": "tag:stelvio:app", "Values": [app]}])[
            "SecurityGroups"
        ]
        == []
    )
    filters = [{"Name": "tag:stelvio:app", "Values": [app]}]
    assert not ec2.describe_addresses(Filters=filters)["Addresses"]
    for operation, field in (
        ("describe_subnets", "Subnets"),
        ("describe_route_tables", "RouteTables"),
        ("describe_internet_gateways", "InternetGateways"),
        ("describe_network_interfaces", "NetworkInterfaces"),
        ("describe_volumes", "Volumes"),
    ):
        assert not getattr(ec2, operation)(Filters=filters)[field]
    instances = ec2.describe_instances(Filters=filters)["Reservations"]
    assert all(i["State"]["Name"] == "terminated" for r in instances for i in r["Instances"])
    assert all(
        n["State"] == "deleted"
        for p in ec2.get_paginator("describe_nat_gateways").paginate(Filter=filters)
        for n in p["NatGateways"]
    )
    iam = sdk.client("iam", config=AWS_IO)
    for operation, field, name in (
        ("list_roles", "Roles", "RoleName"),
        ("list_instance_profiles", "InstanceProfiles", "InstanceProfileName"),
    ):
        assert not [
            item[name]
            for page in iam.get_paginator(operation).paginate()
            for item in page[field]
            if item[name].startswith(prefix)
        ]
    assert not [
        item["PolicyName"]
        for page in iam.get_paginator("list_policies").paginate(Scope="Local")
        for item in page["Policies"]
        if item["PolicyName"].startswith(prefix)
    ]
    assert not [
        item["Name"]
        for page in sdk.client("ssm", config=AWS_IO)
        .get_paginator("list_documents")
        .paginate(Filters=[{"Key": "Owner", "Values": ["Self"]}])
        for item in page["DocumentIdentifiers"]
        if item["Name"].startswith(prefix)
    ]


def assert_access_absent(sdk, sessions: set[str], owner_hashes: set[str]) -> None:
    """Every recorded temporary owner and persistent transport is disposed."""
    ec2 = sdk.client("ec2", config=AWS_IO)
    iam = sdk.client("iam", config=AWS_IO)
    ssm = sdk.client("ssm", config=AWS_IO)
    for session in sessions:
        filters = [{"Name": "tag:stlv:tunnel-session", "Values": [session]}]
        instances = ec2.describe_instances(Filters=filters)["Reservations"]
        assert all(i["State"]["Name"] == "terminated" for r in instances for i in r["Instances"])
        for operation, field in (
            ("describe_security_groups", "SecurityGroups"),
            ("describe_network_interfaces", "NetworkInterfaces"),
            ("describe_volumes", "Volumes"),
        ):
            assert not getattr(ec2, operation)(Filters=filters)[field]
        prefix = "stlv-tunnel-" + session.replace("-", "") + "-"
        for operation, field, name in (
            ("list_roles", "Roles", "RoleName"),
            ("list_instance_profiles", "InstanceProfiles", "InstanceProfileName"),
        ):
            assert not [
                i[name]
                for page in iam.get_paginator(operation).paginate()
                for i in page[field]
                if i[name].startswith(prefix)
            ]
        assert not [
            document["Name"]
            for page in ssm.get_paginator("list_documents").paginate(
                Filters=[{"Key": "Owner", "Values": ["Self"]}]
            )
            for document in page["DocumentIdentifiers"]
            if document["Name"].startswith(prefix)
        ]
        assert not [
            parameter["Name"]
            for page in ssm.get_paginator("describe_parameters").paginate(
                ParameterFilters=[
                    {"Key": "Name", "Option": "BeginsWith", "Values": [f"/stlv/tunnel/{session}/"]}
                ]
            )
            for parameter in page["Parameters"]
        ]
    assert not [
        active
        for page in ssm.get_paginator("describe_sessions").paginate(State="Active")
        for active in page["Sessions"]
        if any(active.get("Reason", "").startswith(f"stelvio/{h}/") for h in owner_hashes)
    ]
