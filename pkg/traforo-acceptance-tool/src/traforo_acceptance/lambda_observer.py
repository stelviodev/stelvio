"""Observe the Lambda subclass and API operations without changing candidate source."""

from __future__ import annotations

import base64
import functools
import hashlib
from contextlib import contextmanager
from uuid import uuid4

from botocore.exceptions import ClientError

from .lambda_aws import before_create, capture_proof
from .store import event, write_json


def install(root, session_class, api_class, base_class, registration):
    session_class._acceptance_lambda = True
    deploy = session_class.deploy
    destroy = session_class.destroy
    create = api_class.create
    command = base_class.command
    evidence = session_class.write_evidence
    refusal = session_class.wait_host_key_refusal

    def inventory(session):
        owned = registration(session)
        try:
            event(
                root, "proof_inventory", **owned, snapshot=capture_proof(root, session.aws, owned)
            )
        except Exception as error:
            event(root, "inventory_error", **owned, error=str(error))

    @functools.wraps(deploy)
    def observed_deploy(session):
        owned = registration(session)
        write_json(session.project / ".acceptance-owner.json", owned)
        event(root, "proof_intent", **owned)
        for number, api in session.apis.items():
            api._acceptance_owner = owned
            api._acceptance_sdk = session.aws
            api._acceptance_number = number
        try:
            return deploy(session)
        finally:
            record_topology(session)
            event(root, "proof_outputs", **owned, outputs=getattr(session, "_outputs", {}))
            inventory(session)

    @functools.wraps(create)
    def observed_create(api):
        owned = api._acceptance_owner
        number = api._acceptance_number
        before_create(root, api._acceptance_sdk, owned, number, api.name)
        try:
            return create(api)
        except ClientError as error:
            if error.response["Error"]["Code"] == "TooManyRequestsException":
                event(
                    root,
                    "api_not_created",
                    **owned,
                    number=number,
                    name=api.name,
                    reason="explicit-throttle",
                )
            raise
        finally:
            if api.api_id:
                event(
                    root,
                    "api_created",
                    **owned,
                    number=number,
                    name=api.name,
                    api_id=api.api_id,
                    receipt=str(api.evidence),
                )
            else:
                event(root, "api_unconfirmed", **owned, number=number, name=api.name)

    @functools.wraps(command)
    def observed_command(session, *args, **kwargs):
        if getattr(session, "_acceptance_lambda", False) and args and args[0] == "deploy":
            event(root, "application_deploy_intent", **registration(session))
        return command(session, *args, **kwargs)

    def record_topology(session):
        # Outputs can exist before the manifest is loaded during a failed deploy.
        outputs = getattr(session, "_outputs", {})
        manifest = getattr(session, "_manifest", None)
        targets, vpcs, clients = {}, [], {}
        if manifest is not None:
            for network in manifest.used_vpcs:
                vpcs.append({"id": network.vpc_id, "identity": network.identity})
            for number in session.apis:
                network = session.network(number)
                targets[str(number)] = {
                    "vpc_id": network.vpc_id,
                    "instance_id": network.access.instance_id,
                    "endpoint_id": outputs.get(f"endpoint{number}_id"),
                    "functions": [
                        outputs[f"{kind}{number}_name"]
                        for kind in ("function", "backend")
                        if f"{kind}{number}_name" in outputs
                    ],
                }
        for number in (1, 2, 3, 4):
            name = outputs.get(f"function{number}_name")
            if name:
                vpc = next(
                    (v["id"] for v in vpcs if v["identity"].endswith(f"::net{number}")), None
                )
                clients[str(number)] = {
                    "name": name,
                    "vpc_id": vpc,
                    "networking": "managed"
                    if number in session.apis
                    else "disabled"
                    if number == 3
                    else "none",
                }
        functions = [
            outputs[key]
            for kind, numbers in (("function", (1, 2, 3, 4)), ("backend", (1, 2)))
            for number in numbers
            if (key := f"{kind}{number}_name") in outputs
        ]
        event(
            root,
            "lambda_topology",
            **registration(session),
            targets=targets,
            clients=clients,
            vpcs=vpcs,
            functions=functions,
        )

    @functools.wraps(evidence)
    def observed_evidence(session):
        record_topology(session)
        return evidence(session)

    @functools.wraps(destroy)
    def observed_destroy(session):
        record_topology(session)
        inventory(session)
        result = destroy(session)
        event(root, "proof_destroyed", **registration(session))
        return result

    def fault_wrapper(original, name, instance):
        @functools.wraps(original)
        @contextmanager
        def observed(session, target, *args, **kwargs):
            fault_id = uuid4().hex
            fields = {
                **registration(session),
                "fault_id": fault_id,
                "fault": name,
                "instance_id": instance(session, target),
            }
            event(root, "fault_enter", **fields)
            finished = False
            try:
                with original(session, target, *args, **kwargs) as value:
                    if name == "host-key-rotation" and isinstance(value, str):
                        parts = value.split()
                        if len(parts) == 2:
                            raw = base64.b64decode(parts[1], validate=True)
                            fingerprint = (
                                base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
                            )
                            event(
                                root,
                                "fault_established",
                                **fields,
                                public_fingerprint="SHA256:" + fingerprint,
                            )
                    yield value
                finished = True
            finally:
                event(root, "fault_exit" if finished else "fault_unfinished", **fields)

        return observed

    @functools.wraps(refusal)
    def observed_refusal(session, instance_id):
        fields = {
            **registration(session),
            "instance_id": instance_id,
            "pid": session.process.pid,
            "log": str(session.log_path),
        }
        event(root, "startup_refusal_enter", **fields)
        passed = False
        try:
            result = refusal(session, instance_id)
            passed = True
            return result
        finally:
            event(
                root,
                "startup_refusal_result",
                **fields,
                outcome="passed" if passed else "failed",
                exit_code=session.process.poll(),
            )

    session_class.wait_host_key_refusal = observed_refusal
    session_class.deploy = observed_deploy
    session_class.destroy = observed_destroy
    session_class.write_evidence = observed_evidence
    api_class.create = observed_create
    base_class.command = observed_command
    session_class.stopped_bastion = fault_wrapper(
        session_class.stopped_bastion,
        "bastion-stop",
        lambda session, number: session.network(number).access.instance_id,
    )
    session_class.rotated_host_key = fault_wrapper(
        session_class.rotated_host_key, "host-key-rotation", lambda session, instance: instance
    )
