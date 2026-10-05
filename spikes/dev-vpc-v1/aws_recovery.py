"""P0 intent-before-create recovery, including lost AWS success responses.

A small application VPC/target fixture stays independent of temporary groups
and ingress rules. Crash phases exit immediately after AWS commits, without
recording returned IDs. Recovery uses account-bound remote intent and planned
names/tags, never the original process, venv, or application program.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any
from uuid import UUID

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

CIDR = "10.252.0.0/16"
PORT = 27017
CONFIG = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2})
CASES = ("group", "rule")


class State:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        self.account = session.client("sts", config=CONFIG).get_caller_identity()["Account"]
        self.s3 = session.client("s3", config=CONFIG)
        self.ec2 = session.client("ec2", config=CONFIG)
        self.prefix = f"dev-vpc-proof/{args.owner}/"
        self.s3.head_bucket(Bucket=args.bucket, ExpectedBucketOwner=self.account)
        expected = {
            "version": 1,
            "owner": args.owner,
            "account": self.account,
            "region": args.region,
        }
        if args.command == "init":
            self.put("intent.json", expected)
        if self.get("intent.json") != expected:
            raise RuntimeError("Remote owner/account/region mismatch; mutations refused")

    def put(self, key: str, data: dict) -> None:
        self.s3.put_object(
            Bucket=self.args.bucket,
            Key=self.prefix + key,
            ExpectedBucketOwner=self.account,
            IfNoneMatch="*",
            Body=json.dumps(data, sort_keys=True).encode(),
            ContentType="application/json",
            ServerSideEncryption="AES256",
        )

    def get(self, key: str, *, optional: bool = False) -> dict | None:
        try:
            result = self.s3.get_object(
                Bucket=self.args.bucket, Key=self.prefix + key, ExpectedBucketOwner=self.account
            )
        except self.s3.exceptions.NoSuchKey:
            if optional:
                return None
            raise
        return json.loads(result["Body"].read())

    def tags(self, unit: str) -> list[dict[str, str]]:
        return [
            {"Key": "stlv:proof-owner", "Value": self.args.owner},
            {"Key": "stlv:proof-unit", "Value": unit},
        ]

    def filters(self, unit: str) -> list[dict[str, Any]]:
        return [{"Name": f"tag:{tag['Key']}", "Values": [tag["Value"]]} for tag in self.tags(unit)]

    def group(self, vpc: str, name: str, unit: str, description: str) -> dict | None:
        groups = self.ec2.describe_security_groups(
            Filters=[
                *self.filters(unit),
                {"Name": "vpc-id", "Values": [vpc]},
                {"Name": "group-name", "Values": [name]},
            ]
        )["SecurityGroups"]
        if len(groups) > 1 or (groups and groups[0]["Description"] != description):
            raise RuntimeError("Planned group identity is ambiguous; cleanup refused")
        return groups[0] if groups else None

    def create_group(self, vpc: str, name: str, unit: str, description: str) -> str:
        return self.ec2.create_security_group(
            VpcId=vpc,
            GroupName=name,
            Description=description,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": self.tags(unit)}],
        )["GroupId"]


def prepare(state: State) -> None:
    state.put("app-plan.json", {"cidr": CIDR, "target_name": f"stlv-proof-app-{state.args.owner}"})
    vpc = state.ec2.create_vpc(
        CidrBlock=CIDR,
        TagSpecifications=[{"ResourceType": "vpc", "Tags": state.tags("app")}],
    )["Vpc"]["VpcId"]
    state.put("app-vpc.json", {"vpc": vpc})
    state.ec2.get_waiter("vpc_available").wait(
        VpcIds=[vpc], WaiterConfig={"Delay": 2, "MaxAttempts": 60}
    )
    target = state.create_group(
        vpc, f"stlv-proof-app-{state.args.owner}", "app", "P0 recovery application target"
    )
    state.put("app-target.json", {"target": target})


def application(
    state: State, *, allow_missing_target: bool = False
) -> tuple[str | None, dict | None]:
    vpcs = state.ec2.describe_vpcs(Filters=state.filters("app"))["Vpcs"]
    if len(vpcs) > 1 or (vpcs and vpcs[0]["CidrBlock"] != CIDR):
        raise RuntimeError("Application fixture identity is ambiguous")
    if not vpcs:
        return None, None
    vpc = vpcs[0]["VpcId"]
    recorded = state.get("app-vpc.json", optional=True)
    if recorded and recorded != {"vpc": vpc}:
        raise RuntimeError("Application VPC differs from recorded identity")
    target = state.group(
        vpc, f"stlv-proof-app-{state.args.owner}", "app", "P0 recovery application target"
    )
    recorded = state.get("app-target.json", optional=True)
    if recorded and (
        (not target and not allow_missing_target)
        or (target and recorded != {"target": target["GroupId"]})
    ):
        raise RuntimeError("Application target differs from recorded identity")
    return vpc, target


def crash(state: State, case: str) -> None:
    vpc, target = application(state)
    if not vpc or not target:
        raise RuntimeError("Complete application fixture required")
    name = f"stlv-proof-access-{state.args.owner}-{case}"
    plan = {
        "vpc": vpc,
        "target": target["GroupId"],
        "name": name,
        "description": f"P0 lost-response recovery {case}",
        "case": case,
    }
    state.put(f"pending-{case}.json", plan)
    group = state.create_group(vpc, name, case, plan["description"])
    if case == "rule":
        state.ec2.get_waiter("security_group_exists").wait(
            GroupIds=[group], WaiterConfig={"Delay": 2, "MaxAttempts": 60}
        )
        state.ec2.authorize_security_group_ingress(
            GroupId=target["GroupId"],
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": PORT,
                    "ToPort": PORT,
                    "UserIdGroupPairs": [{"GroupId": group}],
                }
            ],
            TagSpecifications=[{"ResourceType": "security-group-rule", "Tags": state.tags(case)}],
        )
    # Deliberately leave no local/remote returned group/rule ID checkpoint.
    os._exit(73 if case == "group" else 74)


def recover_case(
    state: State, case: str, vpc: str, target: dict, *, allow_partial: bool = False
) -> dict:
    plan = state.get(f"pending-{case}.json", optional=True)
    if not plan:
        return {"case": case, "created": False}
    expected = {
        "vpc": vpc,
        "target": target["GroupId"],
        "name": f"stlv-proof-access-{state.args.owner}-{case}",
        "description": f"P0 lost-response recovery {case}",
        "case": case,
    }
    if plan != expected:
        raise RuntimeError("Pending remote resource intent mismatch")
    observed = observe_case(state, case, vpc, plan, allow_partial=allow_partial)
    delete_case(state, case, vpc, plan, observed)
    return confirm_case(state, case, vpc, plan, observed)


def observe_case(
    state: State, case: str, vpc: str, plan: dict, *, allow_partial: bool = False
) -> dict:
    observed_key = f"observed-{case}.json"
    observed = state.get(observed_key, optional=True)
    deadline = time.monotonic() + 120
    while observed is None:
        group = state.group(vpc, plan["name"], case, plan["description"])
        rules = owned_rules(state, case, plan["target"])
        partial = allow_partial and time.monotonic() >= deadline
        if group and (case == "group" or rules or partial):
            if group["IpPermissions"]:
                raise RuntimeError("Temporary group has foreign ingress; deletion refused")
            if len(rules) != (1 if case == "rule" else 0) and not (partial and not rules):
                raise RuntimeError("Unexpected number of planned ingress rules")
            validate_rules(rules, group["GroupId"])
            observed = {
                "plan": plan,
                "group": group["GroupId"],
                "rules": [rule["SecurityGroupRuleId"] for rule in rules],
            }
            # Persist cleanup identities before deleting: a second recovery can
            # distinguish previously deleted effects from effects never observed.
            state.put(observed_key, observed)
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Planned creation was never observed; retain uncertain intent")
        time.sleep(2)
    return observed


def delete_case(state: State, case: str, vpc: str, plan: dict, observed: dict) -> None:
    if observed["plan"] != plan:
        raise RuntimeError("Recorded cleanup identity mismatch")
    group = state.group(vpc, plan["name"], case, plan["description"])
    if group and (group["GroupId"] != observed["group"] or group["IpPermissions"]):
        raise RuntimeError("Temporary group differs from recorded cleanup identity")
    rules = owned_rules(state, case, plan["target"])
    ids = [rule["SecurityGroupRuleId"] for rule in rules]
    validate_rules(rules, observed["group"])
    if not set(ids).issubset(observed["rules"]):
        raise RuntimeError("Unrecorded owned rule; deletion refused")
    if ids:
        state.ec2.revoke_security_group_ingress(GroupId=plan["target"], SecurityGroupRuleIds=ids)
    if group:
        state.ec2.delete_security_group(GroupId=observed["group"])


def confirm_case(state: State, case: str, vpc: str, plan: dict, observed: dict) -> dict:
    deadline = time.monotonic() + 120
    while True:
        # Check the known ID as well as tags, so a stale filtered empty result
        # cannot certify deletion of a resource we have already observed.
        try:
            existing = state.ec2.describe_security_groups(GroupIds=[observed["group"]])[
                "SecurityGroups"
            ]
        except ClientError as error:
            if error.response["Error"]["Code"] != "InvalidGroup.NotFound":
                raise
            existing = []
        if (
            not existing
            and not state.group(vpc, plan["name"], case, plan["description"])
            and not owned_rules(state, case, plan["target"])
        ):
            return {"case": case, "group": observed["group"], "rules": observed["rules"]}
        if time.monotonic() >= deadline:
            raise RuntimeError("Deletion is not confirmed; retain cleanup identities")
        time.sleep(2)


def validate_rules(rules: list[dict], group: str) -> None:
    for rule in rules:
        if (
            rule["IsEgress"]
            or rule["IpProtocol"] != "tcp"
            or rule["FromPort"] != PORT
            or rule["ToPort"] != PORT
            or rule.get("ReferencedGroupInfo", {}).get("GroupId") != group
        ):
            raise RuntimeError("Owned rule differs from planned identity; deletion refused")


def owned_rules(state: State, case: str, target: str) -> list[dict]:
    try:
        return state.ec2.describe_security_group_rules(
            Filters=[*state.filters(case), {"Name": "group-id", "Values": [target]}]
        )["SecurityGroupRules"]
    except ClientError as error:
        if error.response["Error"]["Code"] != "InvalidGroup.NotFound":
            raise
        return []


def recover(state: State) -> list[dict]:
    vpc, target = application(state)
    if not vpc or not target:
        if state.ec2.describe_security_groups(
            Filters=[{"Name": "tag:stlv:proof-owner", "Values": [state.args.owner]}]
        )["SecurityGroups"]:
            raise RuntimeError("Incomplete app fixture with surviving groups; retain intent")
        return []
    result = [recover_case(state, case, vpc, target) for case in CASES]
    # The application identities remain present and its ingress is restored.
    if state.ec2.describe_security_groups(GroupIds=[target["GroupId"]])["SecurityGroups"][0][
        "IpPermissions"
    ]:
        raise RuntimeError("Application target retains ingress after access cleanup")
    if application(state)[0] != vpc:
        raise RuntimeError("Application VPC changed during access recovery")
    return result


def cleanup_app(state: State) -> None:
    vpc, target = application(state, allow_missing_target=True)
    if not vpc:
        return
    recorded_target = state.get("app-target.json", optional=True)
    saved = state.get("cleanup-app-intent.json", optional=True)
    if not recorded_target and saved and saved.get("vpc") == vpc and saved.get("target"):
        recorded_target = {"target": saved["target"]}
    identity = target or ({"GroupId": recorded_target["target"]} if recorded_target else None)
    expected = {"vpc": vpc, "target": identity["GroupId"] if identity else None}
    if saved is None:
        state.put("cleanup-app-intent.json", expected)
    elif saved != expected:
        raise RuntimeError("Application cleanup intent mismatch")
    if identity:
        for case in CASES:
            recover_case(state, case, vpc, identity, allow_partial=True)
    if target:
        state.ec2.delete_security_group(GroupId=target["GroupId"])
    state.ec2.delete_vpc(VpcId=vpc)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=("init", "prepare", "crash-group", "crash-rule", "recover", "cleanup-app"),
    )
    for name in ("owner", "bucket", "region"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--profile")
    args = parser.parse_args()
    if str(UUID(args.owner)) != args.owner or os.geteuid() == 0:
        parser.error("Require canonical proof owner and nonroot AWS process")
    state = State(args)
    result = None
    if args.command == "prepare":
        prepare(state)
    elif args.command.startswith("crash-"):
        crash(state, args.command.removeprefix("crash-"))
    elif args.command == "recover":
        result = recover(state)
    elif args.command == "cleanup-app":
        cleanup_app(state)
    sys.stdout.write(json.dumps({"phase": args.command, "result": result}) + "\n")


if __name__ == "__main__":
    main()
