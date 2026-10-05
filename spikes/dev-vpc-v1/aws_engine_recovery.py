"""P0 interrupted Pulumi update recovery without the creator or application program.

Run serially under an external proof controller. The controller must kill and
verify the original process group is gone before invoking recover; cancellation
must never be used to steal a live engine's backend lock.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import secrets
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

import boto3
import pulumi
import pulumi_aws as aws
from pulumi.automation import (
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    PulumiCommand,
    create_or_select_stack,
    select_stack,
)
from semver import VersionInfo

if TYPE_CHECKING:
    from types import ModuleType

    from aws_recovery import State

PROJECT = "stlv-vpc-engine-proof"


def load_state(args: argparse.Namespace) -> tuple[ModuleType, State]:
    spec = importlib.util.spec_from_file_location(
        "proof_state", Path(__file__).with_name("aws_recovery.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module.State(args)


def workspace(args: argparse.Namespace, state: State) -> LocalWorkspaceOptions:
    session = aws_session(args)
    ssm = session.client("ssm")
    parameter = f"/stlv/dev-vpc-proof/{args.owner}/passphrase"
    if args.command == "engine-init":
        ssm.put_parameter(Name=parameter, Type="SecureString", Value=secrets.token_urlsafe(48))
    phrase = ssm.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
    credentials = session.get_credentials().get_frozen_credentials()
    return LocalWorkspaceOptions(
        pulumi_command=PulumiCommand(
            root=args.pulumi_root, version=VersionInfo.parse(version("pulumi"))
        ),
        pulumi_home=str(Path(args.pulumi_root) / ".pulumi"),
        project_settings=ProjectSettings(
            name=PROJECT,
            runtime="python",
            backend=ProjectBackend(
                f"s3://{args.bucket}/{state.prefix}engine-state?region={args.region}"
            ),
        ),
        env_vars={
            "PULUMI_CONFIG_PASSPHRASE": phrase,
            "AWS_REGION": args.region,
            "AWS_DEFAULT_REGION": args.region,
            "AWS_ACCESS_KEY_ID": credentials.access_key,
            "AWS_SECRET_ACCESS_KEY": credentials.secret_key,
            "AWS_SESSION_TOKEN": credentials.token or "",
        },
    )


def aws_session(args: argparse.Namespace) -> boto3.Session:
    return boto3.Session(profile_name=args.profile, region_name=args.region)


def program(args: argparse.Namespace, state: State, vpc: str, target: str) -> None:
    provider = aws.Provider(
        "proof-aws",
        region=args.region,
        profile=args.profile,
        allowed_account_ids=[state.account],
    )
    opts = pulumi.ResourceOptions(provider=provider)
    group = aws.ec2.SecurityGroup(
        "access",
        name=f"stlv-proof-engine-{args.owner}",
        vpc_id=vpc,
        description="P0 interrupted-engine access",
        tags={"stlv:proof-owner": args.owner, "stlv:proof-unit": "engine"},
        opts=opts,
    )
    rule = aws.vpc.SecurityGroupIngressRule(
        "ingress",
        security_group_id=target,
        referenced_security_group_id=group.id,
        ip_protocol="tcp",
        from_port=27017,
        to_port=27017,
        tags={"stlv:proof-owner": args.owner, "stlv:proof-unit": "engine"},
        opts=opts,
    )

    def pause(ids: list[str]) -> None:
        state.put("engine-created.json", {"group": ids[0], "rule": ids[1]})
        # The parent kills this process group after seeing the remote marker,
        # while the update remains in progress. The callback never finishes.
        time.sleep(300)
        raise RuntimeError("Controller failed to interrupt engine within five minutes")

    pulumi.Output.all(group.id, rule.id).apply(pause)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("engine-init", "engine-up", "engine-recover"))
    for name in ("owner", "bucket", "region", "pulumi-root"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()
    if str(UUID(args.owner)) != args.owner or os.geteuid() == 0:
        parser.error("Require canonical proof owner and nonroot process")
    module, state = load_state(args)
    opts = workspace(args, state)
    if args.command == "engine-init":
        sys.stdout.write(json.dumps({"phase": args.command}) + "\n")
        return
    vpc, target = module.application(state)
    if not vpc or not target:
        raise RuntimeError("Original application fixture required")
    factory = create_or_select_stack if args.command == "engine-up" else select_stack
    stack = factory(
        stack_name="access",
        project_name=PROJECT,
        program=(lambda: program(args, state, vpc, target["GroupId"]))
        if args.command == "engine-up"
        else (lambda: None),
        opts=opts,
    )
    if args.command == "engine-up":
        stack.up(on_output=print)
        raise RuntimeError("Expected controller interruption")
    # The serial controller has proved the prior owner is dead. Backend cancel
    # belongs only to this unique proof namespace, never the application stack.
    stack.cancel()
    before = stack.export_stack().deployment
    if before.get("pending_operations"):
        raise RuntimeError("Pending creates require identity reconciliation; retain state")
    stack.refresh(on_output=print)
    stack.destroy(on_output=print)
    after = stack.export_stack().deployment
    if any(item.get("custom") for item in after.get("resources", [])):
        raise RuntimeError("Engine access resources remain")
    actual_vpc, actual_target = module.application(state)
    if actual_vpc != vpc or actual_target["GroupId"] != target["GroupId"]:
        raise RuntimeError("Application identity changed")
    if actual_target["IpPermissions"]:
        raise RuntimeError("Application ingress not restored")
    state.put("engine-recovered.json", {"vpc": vpc, "target": target["GroupId"]})
    sys.stdout.write(json.dumps({"phase": args.command, "result": "PASS"}) + "\n")


if __name__ == "__main__":
    main()
