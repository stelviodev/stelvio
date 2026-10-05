"""Discover access-owned AWS effects, including creates absent from Pulumi state."""

from __future__ import annotations

from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    import boto3

    from stelvio.tunnel.access_state import AccessJournal


class AccessInventory:
    def __init__(self, journal: AccessJournal, session: boto3.Session) -> None:
        self.journal = journal
        self.session = session
        self.ec2 = session.client("ec2", region_name=journal.intent.region)
        self.iam = session.client("iam")
        self.ssm = session.client("ssm", region_name=journal.intent.region)

    def validate_owner(self) -> None:
        self.journal.require_claim()
        if (
            self.session.client("sts").get_caller_identity()["Account"]
            != self.journal.intent.account
        ):
            raise RuntimeError("Temporary access AWS account changed; mutations refused")

    def _filters(self) -> list[dict]:
        return [
            {"Name": f"tag:{key}", "Values": [value]}
            for key, value in self.journal.intent.tags.items()
        ]

    def _tags(self, tags: list[dict]) -> None:
        values = {tag["Key"]: tag["Value"] for tag in tags}
        if any(values.get(key) != value for key, value in self.journal.intent.tags.items()):
            raise RuntimeError("Temporary AWS resource ownership differs; cleanup refused")

    @staticmethod
    def _one(values: list[dict], kind: str) -> dict | None:
        if len(values) > 1:
            raise RuntimeError(f"Ambiguous temporary {kind} ownership; cleanup refused")
        return values[0] if values else None

    def group(self) -> dict | None:
        intent = self.journal.intent
        groups = self.ec2.describe_security_groups(Filters=self._filters())["SecurityGroups"]
        group = self._one(groups, "security group")
        if group:
            self._tags(group.get("Tags", []))
            if (
                group["VpcId"] != intent.vpc_id
                or group["OwnerId"] != intent.account
                or group["GroupName"] != intent.name + "-sg"
                or group.get("IpPermissions")
            ):
                raise RuntimeError("Temporary security group differs from intent; cleanup refused")
            for permission in group.get("IpPermissionsEgress", []):
                if permission != {
                    "IpProtocol": "-1",
                    "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                    "Ipv6Ranges": [],
                    "PrefixListIds": [],
                    "UserIdGroupPairs": [],
                }:
                    raise RuntimeError(
                        "Temporary security group has foreign egress; cleanup refused"
                    )
        return group

    def instance(self, group: dict | None) -> dict | None:
        intent = self.journal.intent
        pages = self.ec2.get_paginator("describe_instances").paginate(Filters=self._filters())
        instances = [
            instance
            for page in pages
            for reservation in page["Reservations"]
            for instance in reservation["Instances"]
        ]
        instance = self._one(instances, "instance")
        if instance:
            self._tags(instance.get("Tags", []))
            if (
                instance["VpcId"] != intent.vpc_id
                or instance["SubnetId"] != intent.subnet_id
                or instance["ImageId"] != intent.ami
                or instance["Placement"]["AvailabilityZone"] != intent.availability_zone
                or (
                    instance["State"]["Name"] != "terminated"
                    and (
                        not group
                        or {item["GroupId"] for item in instance["SecurityGroups"]}
                        != {group["GroupId"]}
                    )
                )
            ):
                raise RuntimeError("Temporary instance differs from intent; cleanup refused")
        return instance

    def rules(self, group: dict | None) -> list[dict]:
        intent = self.journal.intent
        pages = self.ec2.get_paginator("describe_security_group_rules").paginate(
            Filters=self._filters()
        )
        rules = [rule for page in pages for rule in page["SecurityGroupRules"]]
        for rule in rules:
            self._tags(rule.get("Tags", []))
            if not group or rule["GroupOwnerId"] != intent.account:
                raise RuntimeError("Temporary rule source ownership unavailable; cleanup refused")
            if rule["IsEgress"]:
                matches = (
                    rule["GroupId"] == group["GroupId"]
                    and rule["IpProtocol"] == "-1"
                    and rule.get("CidrIpv4") == "0.0.0.0/0"
                    and not rule.get("CidrIpv6")
                    and not rule.get("ReferencedGroupInfo")
                    and not rule.get("PrefixListId")
                )
            else:
                matches = (
                    rule["IpProtocol"] == "tcp"
                    and rule.get("FromPort") == rule.get("ToPort")
                    and any(
                        rule["GroupId"] == target.security_group_id
                        and rule.get("FromPort") == target.port
                        for target in intent.targets
                    )
                    and rule.get("ReferencedGroupInfo", {}).get("GroupId") == group["GroupId"]
                    and not rule.get("CidrIpv4")
                    and not rule.get("CidrIpv6")
                    and not rule.get("PrefixListId")
                )
            if not matches:
                raise RuntimeError("Temporary rule differs from planned ingress; cleanup refused")
        return rules

    def role(self) -> dict | None:
        name = self.journal.intent.name + "-role"
        try:
            role = self.iam.get_role(RoleName=name)["Role"]
        except ClientError as error:
            if error.response["Error"]["Code"] == "NoSuchEntity":
                return None
            raise
        self._tags(self.iam.list_role_tags(RoleName=name)["Tags"])
        if role["RoleName"] != name:
            raise RuntimeError("Temporary role differs from planned identity")
        return role

    def profile(self) -> dict | None:
        name = self.journal.intent.name + "-profile"
        try:
            profile = self.iam.get_instance_profile(InstanceProfileName=name)["InstanceProfile"]
        except ClientError as error:
            if error.response["Error"]["Code"] == "NoSuchEntity":
                return None
            raise
        self._tags(self.iam.list_instance_profile_tags(InstanceProfileName=name)["Tags"])
        if any(
            role["RoleName"] != self.journal.intent.name + "-role" for role in profile["Roles"]
        ):
            raise RuntimeError("Temporary profile contains a foreign role; cleanup refused")
        return profile

    def document(self) -> dict | None:
        name = self.journal.intent.name + "-identity"
        try:
            document = self.ssm.describe_document(Name=name)["Document"]
        except ClientError as error:
            if error.response["Error"]["Code"] == "InvalidDocument":
                return None
            raise
        self._tags(
            self.ssm.list_tags_for_resource(ResourceType="Document", ResourceId=name)["TagList"]
        )
        if document["Name"] != name or document["DocumentType"] != "Command":
            raise RuntimeError("Temporary host identity document differs; cleanup refused")
        return document

    def observe(self) -> dict:
        self.validate_owner()
        group = self.group()
        instance = self.instance(group)
        rules = self.rules(group)
        role = self.role()
        profile = self.profile()
        document = self.document()
        return {
            "group": group["GroupId"] if group else None,
            "instance": instance["InstanceId"] if instance else None,
            "rules": sorted(rule["SecurityGroupRuleId"] for rule in rules),
            "role": role["RoleId"] if role else None,
            "profile": profile["InstanceProfileId"] if profile else None,
            "document": document["Name"] if document else None,
        }
