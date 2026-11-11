"""Deployment-owned persistent AWS access resources.

The VPC component supplies resource ownership and stable names. Networking sessions
consume its deployed descriptor and never call this builder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, final

import pulumi
import pulumi_aws as aws

from stelvio.component import resource_name
from stelvio.network import SSH_USER

if TYPE_CHECKING:
    from collections.abc import Callable

AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"


def user_data() -> str:
    return f"""#!/bin/bash
set -euo pipefail
useradd --create-home --shell /sbin/nologin --password '*' {SSH_USER}
cat > /etc/ssh/sshd_config.d/05-stelvio-tunnel.conf <<'SSH'
ListenAddress 127.0.0.1
Match User {SSH_USER}
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    MaxSessions 0
    AllowTcpForwarding local
    PermitOpen any
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no
SSH
/usr/sbin/sshd -t
systemctl restart sshd
"""


def ssm_policy() -> str:
    return json.dumps(
        {
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
                        "ssmmessages:CreateControlChannel",
                        "ssmmessages:CreateDataChannel",
                        "ssmmessages:OpenControlChannel",
                        "ssmmessages:OpenDataChannel",
                    ],
                    "Resource": "*",
                }
            ],
        }
    )


def assume_role_policy() -> str:
    return json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                }
            ],
        }
    )


@final
@dataclass(frozen=True)
class BastionResources:
    instance: aws.ec2.Instance
    security_group: aws.ec2.SecurityGroup


def create_bastion(  # noqa: PLR0913  # explicit deployment ownership and network inputs
    *,
    name: str,
    vpc_id: pulumi.Input[str],
    subnet_id: pulumi.Input[str],
    tags: dict[str, str],
    opts: pulumi.ResourceOptions,
    customize: Callable[..., dict[str, Any]],
) -> BastionResources:
    """Build SSM-only access in a public subnet, without any SSH ingress."""

    def label(suffix: str, limit: int = 64) -> str:
        return resource_name(name, suffix=suffix, limit=limit)

    group = aws.ec2.SecurityGroup(
        label("-bastion-sg", 255),
        **customize(
            "bastion_security_group",
            {"tags": {"Name": label("-bastion-sg", 255)}},
            default_props={"vpc_id": vpc_id, "description": "Stelvio SSM access: no inbound SSH"},
            inject_tags=True,
        ),
        opts=opts,
    )
    egress = aws.vpc.SecurityGroupEgressRule(
        label("-bastion-egress"),
        **customize(
            "bastion_egress",
            {},
            default_props={
                "security_group_id": group.id,
                "ip_protocol": "-1",
                "cidr_ipv4": "0.0.0.0/0",
            },
            inject_tags=True,
        ),
        opts=opts,
    )
    role = aws.iam.Role(
        label("-bastion-role"),
        **customize(
            "bastion_role",
            {},
            default_props={"assume_role_policy": assume_role_policy()},
            inject_tags=True,
        ),
        opts=opts,
    )
    policy = aws.iam.RolePolicy(
        label("-bastion-ssm"),
        **customize(
            "bastion_policy",
            {},
            default_props={"role": role.id, "policy": ssm_policy()},
        ),
        opts=opts,
    )
    profile = aws.iam.InstanceProfile(
        label("-bastion-profile", 128),
        **customize("bastion_profile", {}, default_props={"role": role.name}, inject_tags=True),
        opts=opts,
    )
    ami = aws.ssm.get_parameter_output(
        name=AMI_PARAMETER, opts=pulumi.InvokeOutputOptions(provider=opts.provider)
    ).value
    instance = aws.ec2.Instance(
        label("-bastion"),
        **customize(
            "bastion",
            {"tags": {"Name": label("-bastion")}},
            default_props={
                "ami": ami,
                "instance_type": "t4g.nano",
                "subnet_id": subnet_id,
                "associate_public_ip_address": True,
                "vpc_security_group_ids": [group.id],
                "iam_instance_profile": profile.name,
                "user_data": user_data(),
                "user_data_replace_on_change": True,
                "metadata_options": {"http_tokens": "required"},
                "root_block_device": {
                    "volume_size": 8,
                    "volume_type": "gp3",
                    "encrypted": True,
                    "delete_on_termination": True,
                },
                "volume_tags": {"Name": label("-bastion")} | tags,
            },
            inject_tags=True,
        ),
        opts=pulumi.ResourceOptions.merge(
            opts, pulumi.ResourceOptions(depends_on=[policy, egress], ignore_changes=["ami"])
        ),
    )
    return BastionResources(instance, group)
