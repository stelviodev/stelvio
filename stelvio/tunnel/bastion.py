"""Shared AWS access resources, with ownership supplied by the caller.

The component owns persistent resources; an independent access stack owns temporary
ones. This builder never updates a VPC component or the application's checkpoint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, final

import pulumi
import pulumi_aws as aws

from stelvio.component import resource_name

if TYPE_CHECKING:
    from collections.abc import Callable

SSH_USER = "stlv-tunnel"
AMI_PARAMETER = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
_READY = "/run/stelvio-tunnel/ready"


def identity_document() -> str:
    # Fixed read-only command; callers cannot substitute shell code during bootstrap.
    command = f"""i=0
while [ ! -f {_READY} ] && [ "$i" -lt 60 ]; do
    sleep 1
    i=$((i + 1))
done
test -f {_READY} || exit 1
cat /etc/ssh/ssh_host_ed25519_key.pub
cat {_READY}"""
    return json.dumps(
        {
            "schemaVersion": "2.2",
            "description": "Read Stelvio tunnel host identity",
            "mainSteps": [
                {
                    "action": "aws:runShellScript",
                    "name": "identity",
                    "inputs": {"timeoutSeconds": "90", "runCommand": [command]},
                }
            ],
        }
    )


def user_data() -> str:
    return f"""#!/bin/bash
set -euo pipefail
useradd --create-home --shell /bin/bash --password '*' {SSH_USER}
install -d -m 0755 /run/stelvio-tunnel
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
systemctl is-active --quiet sshd
test "$(id -u {SSH_USER})" -ne 0
! id -nG {SSH_USER} | grep -qw wheel
printf 'stelvio-tunnel-ready\\n' > {_READY}
"""


def _ssm_policy() -> str:
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


@final
@dataclass(frozen=True)
class BastionResources:
    instance: aws.ec2.Instance
    security_group: aws.ec2.SecurityGroup
    document: aws.ssm.Document


def create_bastion(  # noqa: PLR0913 - explicit deployment ownership and network inputs
    *,
    name: str,
    vpc_id: pulumi.Input[str],
    subnet_id: pulumi.Input[str],
    tags: dict[str, str],
    opts: pulumi.ResourceOptions,
    customize: Callable[..., dict[str, Any]],
    ami: pulumi.Input[str] | None = None,
) -> BastionResources:
    """Build SSM-only access in a public subnet, without any SSH ingress."""

    def label(suffix: str, limit: int = 64) -> str:
        return resource_name(name, suffix=suffix, limit=limit)

    group = aws.ec2.SecurityGroup(
        label("-bastion-sg", 255),
        **customize(
            "bastion_security_group",
            {"vpc_id": vpc_id, "description": "Stelvio SSM access: no inbound SSH"},
            inject_tags=True,
        ),
        opts=opts,
    )
    egress = aws.vpc.SecurityGroupEgressRule(
        label("-bastion-egress"),
        security_group_id=group.id,
        ip_protocol="-1",
        cidr_ipv4="0.0.0.0/0",
        tags=tags or None,
        opts=opts,
    )
    role = aws.iam.Role(
        label("-bastion-role"),
        **customize(
            "bastion_role",
            {
                "assume_role_policy": json.dumps(
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
            },
            inject_tags=True,
        ),
        opts=opts,
    )
    policy = aws.iam.RolePolicy(
        label("-bastion-ssm"), role=role.id, policy=_ssm_policy(), opts=opts
    )
    profile = aws.iam.InstanceProfile(
        label("-bastion-profile", 128),
        **customize("bastion_profile", {"role": role.name}, inject_tags=True),
        opts=opts,
    )
    document = aws.ssm.Document(
        label("-bastion-identity", 128),
        **customize(
            "bastion_identity_document",
            {"document_type": "Command", "content": identity_document()},
            inject_tags=True,
        ),
        opts=opts,
    )
    if ami is None:
        ami = aws.ssm.get_parameter_output(
            name=AMI_PARAMETER, opts=pulumi.InvokeOutputOptions(provider=opts.provider)
        ).value
    instance = aws.ec2.Instance(
        label("-bastion"),
        **customize(
            "bastion",
            {
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
                "volume_tags": tags or None,
            },
            inject_tags=True,
        ),
        opts=pulumi.ResourceOptions.merge(
            opts, pulumi.ResourceOptions(depends_on=[policy, egress])
        ),
    )
    return BastionResources(instance, group, document)
