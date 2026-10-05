"""Manual P0 proof. No AWS operation occurs until an explicit subcommand runs.

Keep this script available in a second checkout/venv for recovery. The application
fixture and temporary ingress live in separate stacks in an owner-specific S3
backend. This never imports the user's app or reads/writes its deployment state.
Run serially: cross-stack orchestration locks are a later production obligation.
"""

from __future__ import annotations

import argparse
import json
import secrets
from contextlib import suppress
from importlib.metadata import version
from typing import TYPE_CHECKING, Any
from uuid import UUID

import boto3
import pulumi
import pulumi_aws as aws
from botocore.exceptions import ClientError
from pulumi.automation import (
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    PulumiCommand,
    Stack,
    StackNotFoundError,
    create_or_select_stack,
    select_stack,
)
from rich.console import Console
from semver import VersionInfo

from stelvio.aws.vpc import Vpc
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.provider import ProviderStore
from stelvio.pulumi import ensure_pulumi, get_stelvio_config_dir

if TYPE_CHECKING:
    from collections.abc import Callable

PROJECT = "stlv-vpc-proof"
COMMANDS = ("init", "app-up", "access-up", "access-destroy", "verify", "app-destroy")
console = Console(markup=False)


def app_program(owner: str, region: str, profile: str | None, account: str) -> None:
    def require_account(
        args: pulumi.ResourceTransformationArgs,
    ) -> pulumi.ResourceTransformationResult | None:
        if args.type_ == "pulumi:providers:aws":
            return pulumi.ResourceTransformationResult(
                props={**args.props, "allowed_account_ids": json.dumps([account])}, opts=args.opts
            )
        return None

    # Keep the public Vpc API intact while constraining the fixture provider.
    pulumi.runtime.register_stack_transformation(require_account)
    _ContextStore.clear()
    ProviderStore.reset()
    _ContextStore.set(
        AppContext(
            name=PROJECT,
            env=owner[:8],
            aws=AwsConfig(profile=profile, region=region),
            home="aws",
            tags={"stlv:proof-owner": owner},
        )
    )
    networks = []
    for index, octet in enumerate((254, 253)):

        def subnet(props: dict[str, Any], octet: int = octet) -> dict[str, Any]:
            return {
                **props,
                "cidr_block": props["cidr_block"].replace("10.0.", f"10.{octet}."),
            }

        net = Vpc(
            f"net-{index}",
            customize={
                "vpc": {"cidr_block": f"10.{octet}.0.0/16"},
                "public_subnet": subnet,
                "private_subnet": subnet,
                "isolated_subnet": subnet,
            },
        ).resources
        target = aws.ec2.SecurityGroup(
            f"target-{index}",
            vpc_id=net.vpc.id,
            description="P0 private-service target; no service deployed",
            tags={"stlv:proof-owner": owner},
            opts=pulumi.ResourceOptions(provider=ProviderStore.aws()),
        )
        networks.append(
            pulumi.Output.all(vpc=net.vpc.id, target=target.id, cidr=net.vpc.cidr_block)
        )
    pulumi.export("networks", pulumi.Output.all(*networks))


def access_program(args: argparse.Namespace, account: str, networks: list[dict[str, str]]) -> None:
    owner = args.owner
    provider = aws.Provider(
        "access-aws", region=args.region, profile=args.profile, allowed_account_ids=[account]
    )
    opts = pulumi.ResourceOptions(provider=provider)
    for index, net in enumerate(networks):
        access = aws.ec2.SecurityGroup(
            f"access-{index}",
            vpc_id=net["vpc"],
            description="P0 independently owned temporary access",
            tags={"stlv:proof-owner": owner},
            opts=opts,
        )
        rule = aws.vpc.SecurityGroupIngressRule(
            f"access-rule-{index}",
            security_group_id=net["target"],
            referenced_security_group_id=access.id,
            ip_protocol="tcp",
            from_port=27017,
            to_port=27017,
            tags={"stlv:proof-owner": owner},
            opts=opts,
        )
        pulumi.export(f"access-{index}", access.id)
        pulumi.export(f"rule-{index}", rule.id)


def initialize_owner(args: argparse.Namespace, session: boto3.Session) -> str:
    owner = args.owner
    account = session.client("sts").get_caller_identity()["Account"]
    s3, ssm = session.client("s3"), session.client("ssm")
    prefix = f"dev-vpc-proof/{owner}"
    key = f"{prefix}/intent.json"
    parameter = f"/stlv/dev-vpc-proof/{owner}/passphrase"
    intent = {"version": 1, "owner": owner, "account": account, "region": args.region}

    if args.command == "init":
        # Intent precedes SSM/Pulumi mutations and is never silently replaced.
        try:
            s3.put_object(
                Bucket=args.bucket,
                Key=key,
                Body=json.dumps(intent).encode(),
                ContentType="application/json",
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "PreconditionFailed":
                raise
    saved = json.loads(s3.get_object(Bucket=args.bucket, Key=key)["Body"].read())
    if saved != intent:
        raise RuntimeError("Owner intent/account/region mismatch; refusing mutations")
    if args.command == "init":
        try:
            ssm.get_parameter(Name=parameter)
        except ssm.exceptions.ParameterNotFound:
            if s3.list_objects_v2(Bucket=args.bucket, Prefix=f"{prefix}/state/", MaxKeys=1).get(
                "KeyCount", 0
            ):
                raise RuntimeError(
                    "Recovery key missing for existing state; refusing new key"
                ) from None
            with suppress(ssm.exceptions.ParameterAlreadyExists):
                ssm.put_parameter(
                    Name=parameter, Type="SecureString", Value=secrets.token_urlsafe(48)
                )
        console.print(json.dumps({"owner": owner, "intent": key, "status": "INITIALIZED"}))
    return account


def workspace_options(args: argparse.Namespace, session: boto3.Session) -> LocalWorkspaceOptions:
    parameter = f"/stlv/dev-vpc-proof/{args.owner}/passphrase"
    prefix = f"dev-vpc-proof/{args.owner}"
    ssm = session.client("ssm")

    passphrase = ssm.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
    env = {
        "PULUMI_CONFIG_PASSPHRASE": passphrase,
        "AWS_REGION": args.region,
        "AWS_DEFAULT_REGION": args.region,
    }
    if args.profile:
        env["AWS_PROFILE"] = args.profile
    # Pin the backend to the identity just validated by boto3. Credentials remain
    # solely in the non-root child's environment, never in intent or checkpoints.
    credentials = session.get_credentials().get_frozen_credentials()
    env.update(
        AWS_ACCESS_KEY_ID=credentials.access_key,
        AWS_SECRET_ACCESS_KEY=credentials.secret_key,
        AWS_SESSION_TOKEN=credentials.token or "",
    )
    ensure_pulumi(show_status=False)
    return LocalWorkspaceOptions(
        pulumi_command=PulumiCommand(
            root=str(get_stelvio_config_dir()), version=VersionInfo.parse(version("pulumi"))
        ),
        pulumi_home=str(get_stelvio_config_dir() / ".pulumi"),
        project_settings=ProjectSettings(
            name=PROJECT,
            runtime="python",
            backend=ProjectBackend(f"s3://{args.bucket}/{prefix}/state?region={args.region}"),
        ),
        env_vars=env,
    )


def verify_cleanup(
    args: argparse.Namespace, session: boto3.Session, app: Stack, access: Stack
) -> None:
    baseline = json.loads(
        session.client("s3")
        .get_object(Bucket=args.bucket, Key=f"dev-vpc-proof/{args.owner}/app-baseline.json")[
            "Body"
        ]
        .read()
    )
    if app.export_stack().deployment != baseline:
        raise RuntimeError("Application checkpoint changed; retain all proof state")
    remaining = [res for res in access.export_stack().deployment["resources"] if res.get("custom")]
    if remaining:
        raise RuntimeError("Access stack still owns resources; destroy access first")
    ec2 = session.client("ec2")
    for net in app.outputs()["networks"].value:
        rules = ec2.describe_security_group_rules(
            Filters=[{"Name": "group-id", "Values": [net["target"]]}]
        )["SecurityGroupRules"]
        if any(rule["IsEgress"] is False for rule in rules):
            raise RuntimeError("Target ingress remains; retain state and inspect")
        actual = ec2.describe_vpcs(VpcIds=[net["vpc"]])["Vpcs"][0]
        if actual["CidrBlock"] != net["cidr"]:
            raise RuntimeError("Application VPC no longer matches its recorded output")
    preview = app.preview(on_output=console.print)
    if any(count for operation, count in preview.change_summary.items() if operation != "same"):
        raise RuntimeError("Application preview has changes; retain proof state")
    console.print("PASS: temporary access removed; application checkpoint/VPCs/preview intact")


def run(args: argparse.Namespace, session: boto3.Session, account: str) -> None:
    owner = args.owner
    prefix = f"dev-vpc-proof/{owner}"
    opts = workspace_options(args, session)

    def stack(name: str, program: Callable[[], None], *, create: bool = False) -> Stack:
        factory = create_or_select_stack if create else select_stack
        return factory(stack_name=name, project_name=PROJECT, program=program, opts=opts)

    if args.command == "access-destroy":
        access = stack("access", lambda: None)
        access.refresh(on_output=console.print)
        access.destroy(on_output=console.print)
        return
    if args.command == "app-destroy":
        try:
            access = stack("access", lambda: None)
        except StackNotFoundError:
            access = None
        if access and any(
            res.get("custom") for res in access.export_stack().deployment.get("resources", [])
        ):
            raise RuntimeError("Access stack still owns resources; destroy access first")
        app = stack("app", lambda: None)
        app.refresh(on_output=console.print)
        app.destroy(on_output=console.print)
        console.print("DESTROYED: fixtures removed; recovery key/checkpoints/intent retained")
        return

    app = stack(
        "app",
        lambda: app_program(owner, args.region, args.profile, account),
        create=args.command == "app-up",
    )
    if args.command == "app-up":
        app.up(on_output=console.print)
        # Store an unchanged-app checkpoint before any access unit is created.
        session.client("s3").put_object(
            Bucket=args.bucket,
            Key=f"{prefix}/app-baseline.json",
            Body=json.dumps(app.export_stack().deployment, sort_keys=True).encode(),
            IfNoneMatch="*",
        )
        console.print(json.dumps({"networks": app.outputs()["networks"].value}))
        return
    networks = app.outputs()["networks"].value
    access = stack(
        "access",
        lambda: access_program(args, account, networks),
        create=args.command == "access-up",
    )
    if args.command == "access-up":
        access.up(on_output=console.print)
    else:
        verify_cleanup(args, session, app, access)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("--owner", required=True, help="Fresh UUID; keep it for recovery")
    parser.add_argument("--bucket", required=True, help="Existing Stelvio AWSHome state bucket")
    parser.add_argument("--region", required=True)
    parser.add_argument("--profile", help="Omit to use the normal AWS credential chain")
    args = parser.parse_args()
    if str(UUID(args.owner)) != args.owner:
        parser.error("owner must be a canonical lowercase UUID")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = initialize_owner(args, session)
    if args.command != "init":
        run(args, session, account)


if __name__ == "__main__":
    main()
