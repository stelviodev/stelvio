"""Discover access-owned AWS effects, including creates absent from Pulumi state."""

from __future__ import annotations

import json
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
            terminated = instance["State"]["Name"] == "terminated"
            actual = (
                instance.get("VpcId"),
                instance.get("SubnetId"),
                instance.get("ImageId"),
                instance.get("Placement", {}).get("AvailabilityZone"),
            )
            expected = (intent.vpc_id, intent.subnet_id, intent.ami, intent.availability_zone)
            if terminated and None in actual:
                # EC2 removes network fields after termination. Only an exact ID
                # observed before cleanup may substitute for those lost fields;
                # copied tags on an unknown terminated instance are insufficient.
                cleanup = self.journal.read("cleanup-observed.json") or {}
                finished = self.journal.read("creation-finished.json") or {}
                known = {cleanup.get("instance"), finished.get("observed", {}).get("instance")} - {
                    None
                }
                if known != {instance["InstanceId"]}:
                    raise RuntimeError(
                        "Terminated instance lacks its recorded identity; cleanup refused"
                    )
            if (
                any(
                    value != wanted and (not terminated or value is not None)
                    for value, wanted in zip(actual, expected, strict=True)
                )
                or instance.get("KeyName")
                or (
                    not terminated
                    and instance.get("MetadataOptions", {}).get("HttpTokens") != "required"
                )
                or (
                    not terminated
                    and instance.get("IamInstanceProfile", {}).get("Arn")
                    != f"arn:aws:iam::{intent.account}:instance-profile/{intent.name}-profile"
                )
                or (
                    not terminated
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

    def volumes(self, instance: dict | None) -> list[dict]:
        volumes = self.ec2.describe_volumes(Filters=self._filters())["Volumes"]
        mapped = (
            {
                mapping["Ebs"]["VolumeId"]
                for mapping in instance.get("BlockDeviceMappings", [])
                if mapping.get("Ebs")
            }
            if instance
            else set()
        )
        missing = mapped - {volume["VolumeId"] for volume in volumes}
        if missing:
            for identity in sorted(missing):
                try:
                    volumes.extend(self.ec2.describe_volumes(VolumeIds=[identity])["Volumes"])
                except ClientError as error:
                    if error.response["Error"]["Code"] != "InvalidVolume.NotFound":
                        raise
        self._one(volumes, "root volume")
        for volume in volumes:
            self.validate_volume(volume, instance["InstanceId"] if instance else None)
        return volumes

    def validate_volume(
        self, volume: dict, owned_instance: str | None, *, recorded: bool = False
    ) -> None:
        tags = {tag["Key"]: tag["Value"] for tag in volume.get("Tags", [])}
        if any(
            key in tags and tags[key] != value for key, value in self.journal.intent.tags.items()
        ):
            raise RuntimeError("Temporary root volume ownership changed; cleanup refused")
        attachments = volume["Attachments"]
        if any(attachment["InstanceId"] != owned_instance for attachment in attachments):
            raise RuntimeError("Temporary root volume has foreign attachment; cleanup refused")
        # Unrecorded root disks can be discovered through their owned instance
        # even if provider volume tagging had not yet completed.
        if not attachments and not recorded:
            self._tags(volume.get("Tags", []))
        if not volume["Encrypted"]:
            raise RuntimeError("Temporary root volume differs from encrypted profile")

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
        if role["AssumeRolePolicyDocument"] != json.loads(
            self.journal.intent.assume_role_policy_content
        ):
            raise RuntimeError("Temporary role trust changed; cleanup refused")
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
        content = self.ssm.get_document(Name=name, DocumentFormat="JSON")["Content"]
        if json.loads(content) != json.loads(self.journal.intent.identity_document_content):
            raise RuntimeError("Temporary host identity command changed; cleanup refused")
        return document

    def observe(self) -> dict:
        self.validate_owner()
        group = self.group()
        instance = self.instance(group)
        volumes = self.volumes(instance)
        rules = self.rules(group)
        role = self.role()
        profile = self.profile()
        document = self.document()
        return {
            "group": group["GroupId"] if group else None,
            "instance": instance["InstanceId"] if instance else None,
            "volumes": sorted(volume["VolumeId"] for volume in volumes),
            "rules": sorted(rule["SecurityGroupRuleId"] for rule in rules),
            "role": role["RoleId"] if role else None,
            "profile": profile["InstanceProfileId"] if profile else None,
            "document": self.document_identity(document) if document else None,
        }

    @staticmethod
    def document_identity(document: dict) -> dict:
        return {
            "name": document["Name"],
            "created": document["CreatedDate"].isoformat(),
            "hash": document["Hash"],
            "version": document["DefaultVersion"],
        }
