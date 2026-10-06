"""Ownership-checked SDK cleanup of temporary access, including unrecorded creates.

The lifecycle controller must stop every creating engine/provider before using
this boundary. A held remote claim alone does not prove an old engine is dead.
"""

from __future__ import annotations

import json
import time
from hashlib import sha256
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from collections.abc import Callable

    from stelvio.tunnel.access_inventory import AccessInventory

WAIT_SECONDS = 120


class AccessCleanup:
    def __init__(self, inventory: AccessInventory, require_stopped: Callable[[], None]) -> None:
        self.inventory = inventory
        self.journal = inventory.journal
        self.require_stopped = require_stopped

    def _check(self) -> None:
        self.require_stopped()
        self.inventory.validate_owner()

    @staticmethod
    def _missing(call: Callable[[], dict], codes: set[str]) -> dict | None:
        try:
            return call()
        except ClientError as error:
            if error.response["Error"]["Code"] not in codes:
                raise
            return None

    def _wait(self, absent: Callable[[], bool], name: str) -> None:
        deadline = time.monotonic() + WAIT_SECONDS
        while not absent():
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Temporary {name} deletion not confirmed; retain recovery records"
                )
            time.sleep(2)

    def _instance_absent(self, instance_id: str) -> bool:
        result = self._missing(
            lambda: self.inventory.ec2.describe_instances(InstanceIds=[instance_id]),
            {"InvalidInstanceID.NotFound"},
        )
        if result is None:
            return True
        instances = [
            item for reservation in result["Reservations"] for item in reservation["Instances"]
        ]
        return not instances or all(
            instance["State"]["Name"] == "terminated" for instance in instances
        )

    def _remove_instance(self, observed: dict) -> None:
        instance_id = observed["instance"]
        if not instance_id or self._instance_absent(instance_id):
            return
        self._check()
        current = self.inventory.instance(self.inventory.group())
        if not current or current["InstanceId"] != instance_id:
            raise RuntimeError("Temporary instance differs from observed identity")
        self.inventory.ec2.terminate_instances(InstanceIds=[instance_id])
        self._wait(lambda: self._instance_absent(instance_id), "instance")

    def _remove_rules(self, observed: dict) -> None:
        for identity in observed["rules"]:
            self._check()
            current = self.inventory.rules(self.inventory.group())
            matches = [rule for rule in current if rule["SecurityGroupRuleId"] == identity]
            if not matches:
                exact = self._missing(
                    lambda identity=identity: self.inventory.ec2.describe_security_group_rules(
                        SecurityGroupRuleIds=[identity]
                    ),
                    {"InvalidSecurityGroupRuleId.NotFound"},
                )
                if exact and exact["SecurityGroupRules"]:
                    raise RuntimeError("Known temporary rule lost ownership; cleanup refused")
                continue
            rule = matches[0]
            revoke = (
                self.inventory.ec2.revoke_security_group_egress
                if rule["IsEgress"]
                else self.inventory.ec2.revoke_security_group_ingress
            )
            revoke(GroupId=rule["GroupId"], SecurityGroupRuleIds=[identity])
            self._wait(
                lambda identity=identity: self._rule_absent(identity), "security group rule"
            )

    def _remove_volumes(self, observed: dict) -> None:
        for identity in observed["volumes"]:
            self._check()
            result = self._missing(
                lambda identity=identity: self.inventory.ec2.describe_volumes(
                    VolumeIds=[identity]
                ),
                {"InvalidVolume.NotFound"},
            )
            if not result or not result["Volumes"]:
                continue
            volume = result["Volumes"][0]
            self.inventory.validate_volume(volume, observed["instance"], recorded=True)
            if volume["Attachments"]:
                raise RuntimeError(
                    "Temporary root volume remains attached; retain recovery records"
                )
            self.inventory.ec2.delete_volume(VolumeId=identity)
            self._wait(lambda identity=identity: self._volume_absent(identity), "root volume")

    def _volume_absent(self, identity: str) -> bool:
        result = self._missing(
            lambda: self.inventory.ec2.describe_volumes(VolumeIds=[identity]),
            {"InvalidVolume.NotFound"},
        )
        return result is None or not result["Volumes"]

    def _rule_absent(self, identity: str) -> bool:
        result = self._missing(
            lambda: self.inventory.ec2.describe_security_group_rules(
                SecurityGroupRuleIds=[identity]
            ),
            {"InvalidSecurityGroupRuleId.NotFound"},
        )
        return result is None or not result["SecurityGroupRules"]

    def _remove_group(self, observed: dict) -> None:
        identity = observed["group"]
        if not identity:
            return
        self._check()
        current = self.inventory.group()
        if not current:
            if not self._group_absent(identity):
                raise RuntimeError("Known temporary group lost ownership; cleanup refused")
            return
        if current["GroupId"] != identity:
            raise RuntimeError("Temporary group differs from observed identity")
        self.inventory.ec2.delete_security_group(GroupId=identity)
        self._wait(lambda: self._group_absent(identity), "security group")

    def _group_absent(self, identity: str) -> bool:
        result = self._missing(
            lambda: self.inventory.ec2.describe_security_groups(GroupIds=[identity]),
            {"InvalidGroup.NotFound"},
        )
        return result is None or not result["SecurityGroups"]

    def _remove_profile(self, observed: dict) -> None:
        if not observed["profile"]:
            return
        self._check()
        profile = self.inventory.profile()
        if not profile:
            return
        if profile["InstanceProfileId"] != observed["profile"]:
            raise RuntimeError("Temporary instance profile differs from observed identity")
        self._wait(
            lambda: self._profile_unused(profile["Arn"], observed["instance"]),
            "instance profile association",
        )
        name = profile["InstanceProfileName"]
        for role in profile["Roles"]:
            self.inventory.iam.remove_role_from_instance_profile(
                InstanceProfileName=name, RoleName=role["RoleName"]
            )
        self.inventory.iam.delete_instance_profile(InstanceProfileName=name)
        self._wait(lambda: self.inventory.profile() is None, "instance profile")

    def _profile_unused(self, arn: str, owned_instance: str | None) -> bool:
        instances = self.inventory.ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "iam-instance-profile.arn", "Values": [arn]}]
        )
        for page in instances:
            for reservation in page["Reservations"]:
                for instance in reservation["Instances"]:
                    if (
                        instance["InstanceId"] != owned_instance
                        and instance["State"]["Name"] != "terminated"
                    ):
                        raise RuntimeError(
                            "Foreign instance uses temporary profile; cleanup refused"
                        )
        pages = self.inventory.ec2.get_paginator(
            "describe_iam_instance_profile_associations"
        ).paginate(
            Filters=[{"Name": "state", "Values": ["associating", "associated", "disassociating"]}]
        )
        active = False
        for page in pages:
            for association in page["IamInstanceProfileAssociations"]:
                if association["IamInstanceProfile"]["Arn"] == arn:
                    if association["InstanceId"] != owned_instance:
                        raise RuntimeError("Foreign profile association exists; cleanup refused")
                    active = True
        return not active

    def _remove_role(self, observed: dict) -> None:
        if not observed["role"]:
            return
        self._check()
        role = self.inventory.role()
        if not role:
            return
        if role["RoleId"] != observed["role"]:
            raise RuntimeError("Temporary role differs from observed identity")
        name = role["RoleName"]
        if (
            self.inventory.iam.list_attached_role_policies(RoleName=name)["AttachedPolicies"]
            or self.inventory.iam.list_instance_profiles_for_role(RoleName=name)[
                "InstanceProfiles"
            ]
        ):
            raise RuntimeError("Temporary role has foreign dependencies; cleanup refused")
        policies = self.inventory.iam.list_role_policies(RoleName=name)["PolicyNames"]
        if len(policies) > 1:
            raise RuntimeError("Temporary role has foreign inline policies; cleanup refused")
        for policy in policies:
            document = self.inventory.iam.get_role_policy(RoleName=name, PolicyName=policy)[
                "PolicyDocument"
            ]
            if document != json.loads(self.journal.intent.ssm_policy_content):
                raise RuntimeError("Temporary role permissions changed; cleanup refused")
            self.inventory.iam.delete_role_policy(RoleName=name, PolicyName=policy)
        self.inventory.iam.delete_role(RoleName=name)
        self._wait(lambda: self.inventory.role() is None, "role")

    def _remove_document(self, observed: dict) -> None:
        if not observed["document"]:
            return
        self._check()
        document = self.inventory.document()
        if not document:
            return
        if self.inventory.document_identity(document) != observed["document"]:
            raise RuntimeError("Temporary document differs from observed identity")
        self.inventory.ssm.delete_document(Name=document["Name"])
        self._wait(lambda: self.inventory.document() is None, "identity document")

    def remove(self) -> dict:
        self._check()
        known = self.journal.read("cleanup-observed.json")
        current = self.inventory.observe()
        if known is None:
            if self.journal.read("creation-finished.json") is None:
                # Stop proof excludes further API submissions; still allow prior
                # committed creates to become discoverable before saving IDs.
                deadline = time.monotonic() + WAIT_SECONDS
                while time.monotonic() < deadline:
                    time.sleep(2)
                    self._check()
                    current = self.inventory.observe()
            if not any(current.values()):
                raise RuntimeError("No temporary creates observed; cleanup outcome is uncertain")
            self.journal.record("cleanup-observed.json", current)
            known = current
        for key, value in current.items():
            if key in {"rules", "volumes"}:
                matches = set(value) <= set(known[key])
            else:
                matches = not value or value == known[key]
            if not matches:
                raise RuntimeError("Temporary ownership differs from saved cleanup identities")
        self._remove_instance(known)
        self._remove_volumes(known)
        self._remove_rules(known)
        self._remove_group(known)
        self._remove_profile(known)
        self._remove_role(known)
        self._remove_document(known)
        final = self.inventory.observe()
        if any(value for key, value in final.items() if key != "instance"):
            raise RuntimeError("Temporary AWS resources remain; retain recovery records")
        if known["instance"] and not self._instance_absent(known["instance"]):
            raise RuntimeError("Temporary instance termination not confirmed")
        self.journal.record(
            "aws-cleaned.json",
            {"observed_sha256": sha256(json.dumps(known, sort_keys=True).encode()).hexdigest()},
        )
        return known
