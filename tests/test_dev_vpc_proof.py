"""Hermetic checks for the manual P0 proof, not production dev-mode acceptance."""

import argparse
import json
from importlib import import_module
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pulumi
from pulumi.automation import StackNotFoundError
from pytest import mark

from tests.aws.pulumi_mocks import R, tid, tn

proof = import_module("spikes.dev-vpc-v1.aws_ownership")
transport = import_module("spikes.dev-vpc-v1.aws_transport")
OWNER = "12345678-1234-4234-8234-123456789abc"
ACCOUNT = "123456789012"


def test_proof_customizes_two_complete_vpc_ranges(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        proof.app_program(OWNER, "us-east-1", "proof-profile", ACCOUNT)

    deploy()

    provider = pulumi_mocks.assert_res("stelvio-aws", R.AWS_PROVIDER, prefixed=False)
    assert json.loads(provider.inputs["allowedAccountIds"]) == [ACCOUNT]
    assert provider.inputs["profile"] == "proof-profile"
    for index, octet in enumerate((254, 253)):
        name = f"stlv-vpc-proof-12345678-net-{index}"
        pulumi_mocks.assert_res(
            name, R.VPC, {"cidrBlock": f"10.{octet}.0.0/16"}, partial=True, prefixed=False
        )
        for tier, third_octets, mask in (
            ("public", (0, 1), 24),
            ("private", (20, 24), 22),
            ("isolated", (60, 61), 24),
        ):
            for az, third in zip(("a", "b"), third_octets, strict=True):
                pulumi_mocks.assert_res(
                    f"{name}-{tier}-subnet-{az}",
                    R.SUBNET,
                    {"cidrBlock": f"10.{octet}.{third}.0/{mask}", "vpcId": tid(name)},
                    partial=True,
                    prefixed=False,
                )
    pulumi_mocks.assert_res_counts(
        {
            R.VPC: 2,
            R.INTERNET_GATEWAY: 2,
            R.DEFAULT_SECURITY_GROUP: 2,
            R.SECURITY_GROUP: 2,
            R.SUBNET: 12,
            R.ROUTE_TABLE: 12,
            R.ROUTE_TABLE_ASSOCIATION: 12,
        }
    )


def test_access_proof_has_only_separate_groups_and_target_rules(pulumi_mocks):
    args = argparse.Namespace(owner=OWNER, region="us-east-1", profile="proof-profile")

    @pulumi.runtime.test
    def deploy():
        proof.access_program(
            args, ACCOUNT, [{"vpc": "vpc-a", "target": "sg-a"}, {"vpc": "vpc-b", "target": "sg-b"}]
        )

    deploy()

    provider = pulumi_mocks.assert_res("access-aws", R.AWS_PROVIDER, prefixed=False)
    assert json.loads(provider.inputs["allowedAccountIds"]) == [ACCOUNT]
    assert provider.inputs["profile"] == "proof-profile"
    for index, (vpc, target) in enumerate((("vpc-a", "sg-a"), ("vpc-b", "sg-b"))):
        pulumi_mocks.assert_res(
            f"access-{index}",
            R.SECURITY_GROUP,
            {
                "vpcId": vpc,
                "description": "P0 independently owned temporary access",
                "tags": {"stlv:proof-owner": OWNER},
            },
            prefixed=False,
        )
        pulumi_mocks.assert_res(
            f"access-rule-{index}",
            R.SECURITY_GROUP_INGRESS_RULE,
            {
                "securityGroupId": target,
                "referencedSecurityGroupId": tid(f"access-{index}"),
                "ipProtocol": "tcp",
                "fromPort": 27017,
                "toPort": 27017,
                "tags": {"stlv:proof-owner": OWNER},
            },
            prefixed=False,
        )
    pulumi_mocks.assert_res_counts({R.SECURITY_GROUP: 2, R.SECURITY_GROUP_INGRESS_RULE: 2})


@mark.parametrize("command", ["app-destroy", "access-destroy"])
def test_recovery_destroys_partial_stack_without_outputs_or_baseline(monkeypatch, command):
    resources = [{"custom": True, "type": "aws:ec2/vpc:Vpc"}]
    stack = Mock()
    stack.refresh.side_effect = lambda **kwargs: None
    stack.destroy.side_effect = lambda **kwargs: resources.clear()
    stack.outputs.side_effect = AssertionError("Partial checkpoint has no completed outputs")
    stack.export_stack.return_value = SimpleNamespace(deployment={"resources": resources})

    def select(*, stack_name, **kwargs):
        if command == "app-destroy" and stack_name == "access":
            raise StackNotFoundError("No access stack was created")
        return stack

    monkeypatch.setattr(proof, "select_stack", select)
    monkeypatch.setattr(proof, "workspace_options", lambda *args: None)
    session = Mock()
    session.client.side_effect = AssertionError(
        "Recovery must not read missing baseline/delete key"
    )
    proof.run(argparse.Namespace(command=command, owner=OWNER), session, ACCOUNT)
    assert resources == []


def test_verify_accepts_destroyed_stack_with_omitted_resources():
    app = Mock()
    app.export_stack.return_value = SimpleNamespace(deployment={"resources": []})
    app.outputs.return_value = {"networks": SimpleNamespace(value=[])}
    app.preview.return_value = SimpleNamespace(change_summary={"same": 1})
    access = Mock()
    access.export_stack.return_value = SimpleNamespace(deployment={})
    session = Mock()
    session.client.return_value.get_object.return_value = {"Body": BytesIO(b'{"resources": []}')}
    proof.verify_cleanup(
        argparse.Namespace(bucket="proof-bucket", owner=OWNER), session, app, access
    )


def test_transport_fixture_has_owned_private_zones_in_each_vpc(pulumi_mocks):
    args = argparse.Namespace(owner=OWNER, region="us-east-1", profile="proof-profile")

    @pulumi.runtime.test
    def deploy():
        transport.app_program(args, ACCOUNT)

    deploy()

    for index, octet in enumerate((254, 253)):
        domain = f"vpc{index}.{OWNER}.stelvio-proof.test"
        pulumi_mocks.assert_res(
            f"private-zone-{index}",
            R.ROUTE53_ZONE,
            {
                "name": domain,
                "comment": "Stelvio P0 private DNS fixture",
                "vpcs": [
                    {
                        "vpcId": tid(f"stlv-vpc-proof-12345678-net-{index}"),
                        "vpcRegion": "us-east-1",
                    }
                ],
                "tags": {"stlv:proof-owner": OWNER},
            },
            prefixed=False,
        )
        pulumi_mocks.assert_res(
            f"private-service-{index}",
            R.ROUTE53_RECORD,
            {
                "name": f"service.{domain}",
                "records": [f"10.{octet}.0.10"],
                "type": "A",
                "ttl": 5,
                "zoneId": tid(f"private-zone-{index}"),
            },
            partial=True,
            prefixed=False,
        )
    pulumi_mocks.assert_res_counts(
        {
            R.VPC: 2,
            R.INTERNET_GATEWAY: 2,
            R.DEFAULT_SECURITY_GROUP: 2,
            R.SECURITY_GROUP: 2,
            R.SUBNET: 12,
            R.ROUTE_TABLE: 12,
            R.ROUTE_TABLE_ASSOCIATION: 12,
            R.ROUTE53_ZONE: 2,
            R.ROUTE53_RECORD: 2,
        }
    )


def test_transport_access_has_no_ssh_ingress_and_only_ephemeral_auth(pulumi_mocks):
    args = argparse.Namespace(
        owner=OWNER, region="us-east-1", profile="proof-profile", ami="ami-proof"
    )
    networks = [
        {"vpc": f"vpc-{i}", "target": f"target-{i}", "public_subnet": f"subnet-{i}", "cidr": cidr}
        for i, cidr in enumerate(("10.254.0.0/16", "10.253.0.0/16"))
    ]

    @pulumi.runtime.test
    def deploy():
        transport.access_program(args, ACCOUNT, networks)

    deploy()

    for index, octet in enumerate((254, 253)):
        pulumi_mocks.assert_res(
            f"bastion-sg-{index}",
            R.SECURITY_GROUP,
            {
                "vpcId": f"vpc-{index}",
                "description": "P0 SSM bastion: no public inbound rules",
                "tags": {"stlv:proof-owner": OWNER},
            },
            prefixed=False,
        )
        pulumi_mocks.assert_res(
            f"echo-access-{index}",
            R.SECURITY_GROUP_INGRESS_RULE,
            {
                "securityGroupId": f"target-{index}",
                "referencedSecurityGroupId": tid(f"bastion-sg-{index}"),
                "ipProtocol": "tcp",
                "fromPort": 8080,
                "toPort": 8080,
                "tags": {"stlv:proof-owner": OWNER},
            },
            prefixed=False,
        )
        instance = pulumi_mocks.assert_res(f"bastion-{index}", R.EC2_INSTANCE, prefixed=False)
        assert instance.inputs["privateIp"] == f"10.{octet}.0.10"
        assert instance.inputs["subnetId"] == f"subnet-{index}"
        assert instance.inputs["vpcSecurityGroupIds"] == [
            tid(f"bastion-sg-{index}"),
            f"target-{index}",
        ]
        assert instance.inputs["metadataOptions"] == {"httpTokens": "required"}
        assert instance.inputs.get("keyName") is None
        assert instance.inputs["rootBlockDevice"]["deleteOnTermination"] is True
        assert instance.inputs["rootBlockDevice"]["encrypted"] is True
        assert instance.inputs["iamInstanceProfile"] == tn("bastion-profile")
        assert instance.inputs["ami"] == "ami-proof"
        assert instance.inputs["instanceType"] == "t4g.nano"
    role = pulumi_mocks.assert_res("bastion-role", R.ROLE, prefixed=False)
    assert json.loads(role.inputs["assumeRolePolicy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    profile = pulumi_mocks.assert_res("bastion-profile", R.INSTANCE_PROFILE, prefixed=False)
    assert profile.inputs["role"] == tn("bastion-role")
    policy = pulumi_mocks.assert_res("bastion-ssm", R.ROLE_POLICY, prefixed=False)
    assert policy.inputs["role"] == tid("bastion-role")
    assert json.loads(policy.inputs["policy"]) == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ssm:UpdateInstanceInformation",
                    "ssm:DescribeDocument",
                    "ssm:GetDocument",
                    "ssm:GetManifest",
                    "ssm:ListAssociations",
                    "ssm:ListInstanceAssociations",
                    "ssm:UpdateAssociationStatus",
                    "ssm:UpdateInstanceAssociationStatus",
                    "ssm:PutInventory",
                    "ssm:PutComplianceItems",
                    "ssm:PutConfigurePackageResult",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "ssmmessages:CreateControlChannel",
                    "ssmmessages:CreateDataChannel",
                    "ssmmessages:OpenControlChannel",
                    "ssmmessages:OpenDataChannel",
                ],
                "Resource": "*",
            },
        ],
    }
    document = pulumi_mocks.assert_res("host-identity", R.SSM_DOCUMENT, prefixed=False)
    assert json.loads(document.inputs["content"]) == {
        "schemaVersion": "2.2",
        "description": "Read P0 host identity",
        "mainSteps": [
            {
                "action": "aws:runShellScript",
                "name": "identity",
                "inputs": {
                    "timeoutSeconds": "90",
                    "runCommand": [
                        'i=0\nwhile [ ! -f /run/stlv-vpc-proof/ready ] && [ "$i" -lt 60 ]; do\n'
                        "    sleep 1\n    i=$((i + 1))\ndone\n"
                        "test -f /run/stlv-vpc-proof/ready || exit 1\n"
                        "cat /etc/ssh/ssh_host_ed25519_key.pub\ncat /run/stlv-vpc-proof/ready"
                    ],
                },
            }
        ],
    }
    assert document.inputs["documentType"] == "Command"
    pulumi_mocks.assert_res_counts(
        {
            R.ROLE: 1,
            R.ROLE_POLICY: 1,
            R.INSTANCE_PROFILE: 1,
            R.SSM_DOCUMENT: 1,
            R.SECURITY_GROUP: 2,
            R.SECURITY_GROUP_EGRESS_RULE: 2,
            R.SECURITY_GROUP_INGRESS_RULE: 2,
            R.EC2_INSTANCE: 2,
        }
    )
