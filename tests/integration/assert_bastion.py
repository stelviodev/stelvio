"""Boto3 read-back assertions for deployment-owned VPC bastions."""

from __future__ import annotations

import base64
import json

from .assert_helpers import _boto3_session
from .assert_vpc import get_security_group


def assert_bastion_resources(
    instance_id: str,
    *,
    vpc_id: str,
    public_subnet_ids: list[str],
    security_group_id: str,
    identity_document: str,
) -> None:
    """Assert the deployed instance, SSM role, identity document, and closed ingress."""
    session = _boto3_session()
    ec2 = session.client("ec2")
    ec2.get_waiter("instance_running").wait(
        InstanceIds=[instance_id], WaiterConfig={"Delay": 10, "MaxAttempts": 30}
    )
    instance = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    assert instance["State"]["Name"] == "running"
    assert instance["InstanceType"] == "t4g.nano"
    assert instance["VpcId"] == vpc_id
    assert instance["SubnetId"] in public_subnet_ids
    assert {group["GroupId"] for group in instance["SecurityGroups"]} == {security_group_id}
    assert instance["MetadataOptions"]["HttpTokens"] == "required"

    instance_profile_arn = instance["IamInstanceProfile"]["Arn"]
    profile_name = instance_profile_arn.rsplit("/", 1)[-1]
    profile = session.client("iam").get_instance_profile(InstanceProfileName=profile_name)[
        "InstanceProfile"
    ]
    roles = profile["Roles"]
    assert len(roles) == 1
    role = roles[0]
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"]["Service"] == (
        "ec2.amazonaws.com"
    )

    iam = session.client("iam")
    policy_names = iam.list_role_policies(RoleName=role["RoleName"])["PolicyNames"]
    assert policy_names
    actions = set()
    for policy_name in policy_names:
        document = iam.get_role_policy(RoleName=role["RoleName"], PolicyName=policy_name)[
            "PolicyDocument"
        ]
        if isinstance(document, str):
            document = json.loads(document)
        for statement in document["Statement"]:
            actions.update(
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
    assert {
        "ssm:UpdateInstanceInformation",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
    } <= actions

    ssm = session.client("ssm")
    described_document = ssm.describe_document(Name=identity_document)["Document"]
    assert described_document["DocumentType"] == "Command"
    assert described_document["Status"] == "Active"
    document = json.loads(ssm.get_document(Name=identity_document)["Content"])
    commands = document["mainSteps"][0]["inputs"]["runCommand"]
    assert any("ssh_host_ed25519_key.pub" in command for command in commands)
    assert any("/run/stelvio-tunnel/ready" in command for command in commands)

    user_data = ec2.describe_instance_attribute(InstanceId=instance_id, Attribute="userData")[
        "UserData"
    ]["Value"]
    bootstrap = base64.b64decode(user_data).decode()
    assert "ListenAddress 127.0.0.1" in bootstrap
    assert "PasswordAuthentication no" in bootstrap

    group = get_security_group(security_group_id)
    assert group["VpcId"] == vpc_id
    assert group["IpPermissions"] == []


def assert_document_db_ingress_from_bastion(
    security_group_id: str,
    *,
    bastion_security_group_id: str,
    app_security_group_id: str,
    port: int,
) -> None:
    """Assert DocumentDB admits only its app and bastion groups on the service port."""
    group = get_security_group(security_group_id)
    actual_sources = set()
    for permission in group["IpPermissions"]:
        assert permission["IpProtocol"] == "tcp"
        assert permission["FromPort"] == port
        assert permission["ToPort"] == port
        assert permission.get("IpRanges", []) == []
        assert permission.get("Ipv6Ranges", []) == []
        actual_sources.update(pair["GroupId"] for pair in permission.get("UserIdGroupPairs", []))
    assert actual_sources == {bastion_security_group_id, app_security_group_id}
