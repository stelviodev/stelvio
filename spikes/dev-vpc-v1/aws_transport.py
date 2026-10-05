"""P0 SSH/SSM fixture. Temporary instances and auth bootstrap belong to access.

No public SSH ingress, no key pair and no durable developer key. The app fixture
owns private zones/records; access can be deleted without rerunning that program.
All commands require an explicit fresh owner, AWS identity and pinned AMI.
"""

from __future__ import annotations

import argparse
import json
from importlib import import_module
from pathlib import Path
from typing import Any
from uuid import UUID

import boto3
import pulumi
import pulumi_aws as aws
from pulumi.automation import create_or_select_stack, select_stack

from stelvio.provider import ProviderStore

ownership = import_module("spikes.dev-vpc-v1.aws_ownership")
USER = "stlv-tunnel"
BOOTSTRAP = """i=0
while [ ! -f /run/stlv-vpc-proof/ready ] && [ "$i" -lt 60 ]; do
    sleep 1
    i=$((i + 1))
done
test -f /run/stlv-vpc-proof/ready || exit 1
cat /etc/ssh/ssh_host_ed25519_key.pub
cat /run/stlv-vpc-proof/ready"""


def identity_document() -> dict[str, Any]:
    return {
        "schemaVersion": "2.2",
        "description": "Read P0 host identity",
        "mainSteps": [
            {
                "action": "aws:runShellScript",
                "name": "identity",
                "inputs": {"timeoutSeconds": "90", "runCommand": [BOOTSTRAP]},
            }
        ],
    }


def user_data(index: int) -> str:
    marker = f"vpc-{index + 1}"
    return f"""#!/bin/bash
set -euo pipefail
useradd --create-home --shell /bin/bash --password '*' {USER}
install -d -m 0755 /opt/stelvio-proof /run/stlv-vpc-proof
cat > /etc/ssh/sshd_config.d/05-stelvio-proof.conf <<'SSH'
ListenAddress 127.0.0.1
Match User {USER}
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
cat > /opt/stelvio-proof/echo.py <<'PY'
import socketserver
class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(30)
        self.request.sendall(b'{marker}\\n')
        size = 0
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            size += len(data)
            if size > 2 * 1024 * 1024:
                return
            self.request.sendall(data)
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
Server(('0.0.0.0', 8080), Echo).serve_forever()
PY
cat > /etc/systemd/system/stelvio-proof.service <<'UNIT'
[Unit]
After=network.target
[Service]
User={USER}
ExecStart=/usr/bin/python3 /opt/stelvio-proof/echo.py
Restart=on-failure
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
[Install]
WantedBy=multi-user.target
UNIT
/usr/sbin/sshd -t
systemctl restart sshd
systemctl daemon-reload
systemctl enable --now stelvio-proof.service
systemctl is-active --quiet sshd stelvio-proof.service
test "$(id -u {USER})" -ne 0
! id -nG {USER} | grep -qw wheel
printf 'proof-ready {marker}\\n' > /run/stlv-vpc-proof/ready
"""


def app_program(args: argparse.Namespace, account: str) -> None:
    networks = ownership.app_program(args.owner, args.region, args.profile, account)
    opts = pulumi.ResourceOptions(provider=ProviderStore.aws())
    for index, (network, octet) in enumerate(zip(networks, (254, 253), strict=True)):
        domain = f"vpc{index}.{args.owner}.stelvio-proof.test"
        zone = aws.route53.Zone(
            f"private-zone-{index}",
            name=domain,
            comment="Stelvio P0 private DNS fixture",
            vpcs=[aws.route53.ZoneVpcArgs(vpc_id=network["vpc"], vpc_region=args.region)],
            tags={"stlv:proof-owner": args.owner},
            opts=opts,
        )
        aws.route53.Record(
            f"private-service-{index}",
            zone_id=zone.id,
            name=f"service.{domain}",
            type="A",
            ttl=5,
            records=[f"10.{octet}.0.10"],
            opts=opts,
        )
        pulumi.export(f"domain-{index}", domain)
        pulumi.export(f"zone-{index}", zone.id)


def access_program(args: argparse.Namespace, account: str, networks: list[dict[str, str]]) -> None:
    provider = aws.Provider(
        "transport-aws", region=args.region, profile=args.profile, allowed_account_ids=[account]
    )
    opts = pulumi.ResourceOptions(provider=provider)
    tags = {"stlv:proof-owner": args.owner}
    role = aws.iam.Role(
        "bastion-role",
        assume_role_policy=json.dumps(
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
        ),
        tags=tags,
        opts=opts,
    )
    policy = aws.iam.RolePolicy(
        "bastion-ssm",
        role=role.id,
        policy=json.dumps(
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
        ),
        opts=opts,
    )
    profile = aws.iam.InstanceProfile("bastion-profile", role=role.name, tags=tags, opts=opts)
    document = aws.ssm.Document(
        "host-identity",
        document_type="Command",
        content=json.dumps(identity_document()),
        tags=tags,
        opts=opts,
    )
    pulumi.export("identity_document", document.name)
    for index, (net, octet) in enumerate(zip(networks, (254, 253), strict=True)):
        group = aws.ec2.SecurityGroup(
            f"bastion-sg-{index}",
            vpc_id=net["vpc"],
            description="P0 SSM bastion: no public inbound rules",
            tags=tags,
            opts=opts,
        )
        egress = aws.vpc.SecurityGroupEgressRule(
            f"bastion-egress-{index}",
            security_group_id=group.id,
            ip_protocol="-1",
            cidr_ipv4="0.0.0.0/0",
            tags=tags,
            opts=opts,
        )
        rule = aws.vpc.SecurityGroupIngressRule(
            f"echo-access-{index}",
            security_group_id=net["target"],
            referenced_security_group_id=group.id,
            ip_protocol="tcp",
            from_port=8080,
            to_port=8080,
            tags=tags,
            opts=opts,
        )
        instance = aws.ec2.Instance(
            f"bastion-{index}",
            ami=args.ami,
            instance_type="t4g.nano",
            subnet_id=net["public_subnet"],
            private_ip=f"10.{octet}.0.10",
            associate_public_ip_address=True,
            vpc_security_group_ids=[group.id, net["target"]],
            iam_instance_profile=profile.name,
            user_data=user_data(index),
            metadata_options=aws.ec2.InstanceMetadataOptionsArgs(http_tokens="required"),
            root_block_device=aws.ec2.InstanceRootBlockDeviceArgs(
                volume_size=8,
                volume_type="gp3",
                encrypted=True,
                delete_on_termination=True,
            ),
            tags=tags,
            volume_tags=tags,
            opts=pulumi.ResourceOptions(provider=provider, depends_on=[policy, egress, rule]),
        )
        pulumi.export(
            f"bastion-{index}",
            pulumi.Output.all(
                instance=instance.id,
                az=instance.availability_zone,
                ip=instance.private_ip,
                cidr=net["cidr"],
                socks=f"127.0.0.1:{10880 + index}",
                ami=args.ami,
            ),
        )


def run(args: argparse.Namespace, session: boto3.Session, account: str) -> None:
    if args.command in ("access-destroy", "app-destroy"):
        ownership.run(args, session, account)
        return
    opts = ownership.workspace_options(args, session)
    if args.command == "app-up":
        app = create_or_select_stack(
            stack_name="app",
            project_name=ownership.PROJECT,
            program=lambda: app_program(args, account),
            opts=opts,
        )
        app.up(on_output=ownership.console.print)
        session.client("s3").put_object(
            Bucket=args.bucket,
            Key=f"dev-vpc-proof/{args.owner}/app-baseline.json",
            Body=json.dumps(app.export_stack().deployment, sort_keys=True).encode(),
            IfNoneMatch="*",
        )
        return
    app = select_stack(
        stack_name="app",
        project_name=ownership.PROJECT,
        program=lambda: app_program(args, account),
        opts=opts,
    )
    networks = app.outputs()["networks"].value
    factory = create_or_select_stack if args.command == "access-up" else select_stack
    access = factory(
        stack_name="access",
        project_name=ownership.PROJECT,
        program=lambda: access_program(args, account, networks),
        opts=opts,
    )
    if args.command == "access-up":
        access.up(on_output=ownership.console.print)
    elif args.command == "verify":
        ownership.verify_cleanup(args, session, app, access)
    else:
        manifest = {
            "owner": args.owner,
            "region": args.region,
            "profile": args.profile,
            "identity_document": access.outputs()["identity_document"].value,
            "bastions": [access.outputs()[f"bastion-{i}"].value for i in range(2)],
            "domains": [app.outputs()[f"domain-{i}"].value for i in range(2)],
        }
        # Only deployment identity/addresses are printed, never auth material.
        args.manifest_out.write_text(json.dumps(manifest, indent=2))
        ownership.console.print(f"Manifest saved: {args.manifest_out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=(*ownership.COMMANDS, "manifest"))
    for name in ("owner", "bucket", "region"):
        parser.add_argument(f"--{name}", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--ami", help="Pinned normal AL2023 arm64 AMI; required for access-up")
    parser.add_argument("--manifest-out", type=Path)
    args = parser.parse_args()
    if str(UUID(args.owner)) != args.owner:
        parser.error("owner must be a canonical lowercase UUID")
    if args.command == "access-up" and not args.ami:
        parser.error("access-up requires --ami")
    if args.command == "manifest" and not args.manifest_out:
        parser.error("manifest requires --manifest-out")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if args.command == "access-up":
        images = session.client("ec2").describe_images(ImageIds=[args.ami], Owners=["amazon"])[
            "Images"
        ]
        if (
            not images
            or images[0]["Architecture"] != "arm64"
            or not images[0]["Name"].startswith("al2023-ami-2023")
        ):
            raise RuntimeError("Require a pinned normal Amazon-owned AL2023 arm64 AMI")
    account = ownership.initialize_owner(args, session)
    if args.command != "init":
        run(args, session, account)


if __name__ == "__main__":
    main()
