"""Component outputs flow through the same state reader used by the dev CLI."""

import json
from dataclasses import replace

import pulumi
from pulumi.runtime import rpc, settings
from pytest import fixture, mark, param, raises

from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import BastionConfig, Vpc
from stelvio.bridge.remote.infrastructure import AppSyncResource
from stelvio.context import _ContextStore, context
from stelvio.stack_outputs import format_outputs, group_outputs, read_network_manifest
from stelvio.tunnel.policy import BastionPolicy
from tests.aws.pulumi_mocks import R, tid


@fixture
def network_state(pulumi_mocks, monkeypatch):
    monitor = settings.get_monitor()
    original = monitor.RegisterResourceOutputs
    resources = []

    def record(request):
        resources.append(
            {
                "urn": request.urn,
                "type": request.urn.split("::")[-2].split("$")[-1],
                "outputs": rpc.deserialize_properties(request.outputs),
            }
        )
        return original(request)

    monkeypatch.setattr(monitor, "RegisterResourceOutputs", record)
    return {"checkpoint": {"latest": {"resources": resources}}}


@mark.parametrize(
    ("value", "policy", "domains"),
    [
        param(..., BastionPolicy.TEMPORARY, (), id="omitted"),
        param(None, BastionPolicy.TEMPORARY, (), id="temporary"),
        param(False, BastionPolicy.DISABLED, (), id="disabled"),
        param(True, BastionPolicy.PERSISTENT, (), id="persistent"),
        param({}, BastionPolicy.PERSISTENT, (), id="empty-dict"),
        param(BastionConfig(), BastionPolicy.PERSISTENT, (), id="empty-config"),
        param(
            {"dns_domains": ["Internal.Example.com.", "internal.example.com"]},
            BastionPolicy.PERSISTENT,
            ("internal.example.com",),
            id="domains",
        ),
    ],
)
def test_vpc_policy_survives_deployment_state(  # noqa: PLR0913 - policy table and boundary fixtures
    value, policy, domains, network_state, pulumi_mocks, monkeypatch
):
    warnings = []
    monkeypatch.setattr(pulumi.log, "warn", lambda message, resource: warnings.append(message))

    @pulumi.runtime.test
    def deploy():
        return Vpc("net", **({} if value is ... else {"bastion": value})).resources

    deploy()
    assert warnings == (
        [
            "Vpc net: bastion=False disables managed access. "
            "Local access to its resources requires your own networking."
        ]
        if policy == BastionPolicy.DISABLED
        else []
    )
    manifest = read_network_manifest(network_state)
    assert len(manifest.vpcs) == 1
    network = manifest.vpcs[0]
    assert network.policy == policy
    assert network.dns_domains == domains
    assert tuple(str(cidr) for cidr in network.cidrs) == ("10.0.0.0/16",)
    assert [str(subnet.cidr) for subnet in network.public_subnets] == [
        "10.0.0.0/24",
        "10.0.1.0/24",
    ]
    assert not manifest.used_vpcs
    assert not format_outputs(group_outputs(network_state))
    persistent = policy == BastionPolicy.PERSISTENT
    if persistent:
        assert network.access.instance_id == tid("test-test-net-bastion")
        assert network.access.security_group_id == tid("test-test-net-bastion-sg")
        assert network.access.availability_zone == "us-east-1a"
        assert network.access.identity_document == "test-test-net-bastion-identity-test-name"
        assert network.access.owner == network.identity
        instance = pulumi_mocks.assert_res(
            "net-bastion",
            R.EC2_INSTANCE,
            {
                "ami": "ami-test-arm64",
                "instanceType": "t4g.nano",
                "subnetId": tid("test-test-net-public-subnet-a"),
                "associatePublicIpAddress": True,
                "vpcSecurityGroupIds": [network.access.security_group_id],
                "iamInstanceProfile": "test-test-net-bastion-profile-test-name",
                "metadataOptions": {"httpTokens": "required"},
                "rootBlockDevice": {
                    "volumeSize": 8,
                    "volumeType": "gp3",
                    "encrypted": True,
                    "deleteOnTermination": True,
                },
                "userDataReplaceOnChange": True,
            },
            partial=True,
        )
        boot = instance.inputs["userData"]
        assert instance.inputs.get("keyName") is None
        ssh_config = boot.split("<<'SSH'\n", 1)[1].split("\nSSH\n", 1)[0]
        assert (
            ssh_config
            == """ListenAddress 127.0.0.1
Match User stlv-tunnel
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    MaxSessions 0
    AllowTcpForwarding local
    PermitOpen any
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
    GatewayPorts no"""
        )
        assert 'test "$(id -u stlv-tunnel)" -ne 0\n! id -nG stlv-tunnel | grep -qw wheel\n' in boot
        pulumi_mocks.assert_res(
            "net-bastion-sg",
            R.SECURITY_GROUP,
            {
                "vpcId": tid("test-test-net"),
                "description": "Stelvio SSM access: no inbound SSH",
            },
        )
        pulumi_mocks.assert_res(
            "net-bastion-egress",
            R.SECURITY_GROUP_EGRESS_RULE,
            {
                "securityGroupId": network.access.security_group_id,
                "ipProtocol": "-1",
                "cidrIpv4": "0.0.0.0/0",
            },
        )
        role_policy = pulumi_mocks.assert_res(
            "net-bastion-ssm",
            R.ROLE_POLICY,
            {"role": tid("test-test-net-bastion-role")},
            partial=True,
        )
        assert json.loads(role_policy.inputs["policy"]) == {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Resource": "*",
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
                }
            ],
        }
        role = pulumi_mocks.assert_res(
            "net-bastion-role",
            R.ROLE,
            {},
            partial=True,
        )
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
        pulumi_mocks.assert_res(
            "net-bastion-profile",
            R.INSTANCE_PROFILE,
            {"role": "test-test-net-bastion-role-test-name"},
        )
        document = pulumi_mocks.assert_res(
            "net-bastion-identity",
            R.SSM_DOCUMENT,
            {"documentType": "Command"},
            partial=True,
        )
        assert json.loads(document.inputs["content"]) == {
            "schemaVersion": "2.2",
            "description": "Read Stelvio tunnel host identity",
            "mainSteps": [
                {
                    "action": "aws:runShellScript",
                    "name": "identity",
                    "inputs": {
                        "timeoutSeconds": "90",
                        "runCommand": [
                            "i=0\nwhile [ ! -f /run/stelvio-tunnel/ready ] "
                            '&& [ "$i" -lt 60 ]; do\n'
                            "    sleep 1\n    i=$((i + 1))\ndone\n"
                            "test -f /run/stelvio-tunnel/ready || exit 1\n"
                            "cat /etc/ssh/ssh_host_ed25519_key.pub\ncat /run/stelvio-tunnel/ready"
                        ],
                    },
                }
            ],
        }
    else:
        assert network.access is None
    pulumi_mocks.assert_res_counts(
        {
            R.VPC: 1,
            R.INTERNET_GATEWAY: 1,
            R.DEFAULT_SECURITY_GROUP: 1,
            R.SUBNET: 6,
            R.ROUTE_TABLE: 6,
            R.ROUTE_TABLE_ASSOCIATION: 6,
            **(
                {
                    R.EC2_INSTANCE: 1,
                    R.SECURITY_GROUP: 1,
                    R.SECURITY_GROUP_EGRESS_RULE: 1,
                    R.ROLE: 1,
                    R.ROLE_POLICY: 1,
                    R.INSTANCE_PROFILE: 1,
                    R.SSM_DOCUMENT: 1,
                }
                if persistent
                else {}
            ),
        }
    )


@mark.parametrize("bastion", [None, True])
@mark.parametrize("security_groups", [None, ["sg-customized-cluster"]])
def test_function_and_documentdb_metadata_preserve_target_and_url(  # noqa: PLR0913
    bastion,
    security_groups,
    network_state,
    pulumi_mocks,
    project_cwd,
    mock_get_or_install_dependencies_function,
):
    @pulumi.runtime.test
    def deploy():
        net = Vpc("net", bastion=bastion)
        cluster = {"port": 27018}
        if security_groups is not None:
            cluster["vpc_security_group_ids"] = security_groups
        database = DocumentDb("db", vpc=net, customize={"cluster": cluster})
        return Function(
            "api",
            handler="functions/simple.handler",
            vpc=net,
            links=[database],
            url="public",
        ).resources

    deploy()
    manifest = read_network_manifest(network_state)
    assert len(manifest.enabled_vpcs) == 1
    net = manifest.vpcs[0]
    resource = manifest.resources[0]
    endpoint = manifest.endpoints[0]
    assert resource.vpc == net.identity
    assert resource.service == "documentdb"
    assert resource.resource_id == tid("test-test-db")
    assert resource.ports == (27018,)
    assert resource.hostnames == (
        f"{tid('test-test-db')}.cluster-us-east-1.docdb.amazonaws.com",
        f"{tid('test-test-db')}.cluster-ro-us-east-1.docdb.amazonaws.com",
    )
    assert resource.security_groups == tuple(security_groups or [tid("test-test-db-sg")])
    assert endpoint.vpcs == (net.identity,)
    assert endpoint.endpoint_id.startswith("api-")
    outputs = format_outputs(group_outputs(network_state))
    assert any(
        f"https://{tid('test-test-api-url')}.lambda-url.us-east-1.on.aws/" in line
        for line in outputs
    )
    assert not any("documentdb" in line or "_stelvio_network" in line for line in outputs)
    if bastion:
        pulumi_mocks.assert_res(
            "db-bastion-ingress-0-0",
            R.SECURITY_GROUP_INGRESS_RULE,
            {
                "securityGroupId": resource.security_groups[0],
                "referencedSecurityGroupId": tid("test-test-net-bastion-sg"),
                "ipProtocol": "tcp",
                "fromPort": 27018,
                "toPort": 27018,
            },
        )
    # Ordinary workload access retains its existing ownership and source.
    pulumi_mocks.assert_res(
        "db-ingress",
        R.SECURITY_GROUP_INGRESS_RULE,
        {
            "securityGroupId": tid("test-test-db-sg"),
            "referencedSecurityGroupId": tid("test-test-net-app-sg"),
            "ipProtocol": "tcp",
            "fromPort": 27018,
            "toPort": 27018,
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.VPC: 1,
            R.INTERNET_GATEWAY: 1,
            R.DEFAULT_SECURITY_GROUP: 1,
            R.SUBNET: 6,
            R.ROUTE_TABLE: 6,
            R.ROUTE_TABLE_ASSOCIATION: 6,
            R.SECURITY_GROUP: 2 + bool(bastion),
            R.SECURITY_GROUP_EGRESS_RULE: 1 + bool(bastion),
            R.SECURITY_GROUP_INGRESS_RULE: 1 + bool(bastion),
            R.DOCDB_CLUSTER: 1,
            R.DOCDB_INSTANCE: 1,
            R.DOCDB_SUBNET_GROUP: 1,
            R.DOCDB_PARAMETER_GROUP: 1,
            R.SECRET_ROTATION: 1,
            R.FUNCTION: 1,
            R.FUNCTION_URL: 1,
            R.ROLE: 1 + bool(bastion),
            R.POLICY: 1,
            R.ROLE_POLICY_ATTACHMENT: 3,
            **(
                {
                    R.EC2_INSTANCE: 1,
                    R.ROLE_POLICY: 1,
                    R.INSTANCE_PROFILE: 1,
                    R.SSM_DOCUMENT: 1,
                }
                if bastion
                else {}
            ),
        }
    )


@mark.parametrize(
    ("value", "error", "message"),
    [
        param(0, TypeError, "'bastion' must be", id="zero-is-not-disabled"),
        param(1, TypeError, "'bastion' must be", id="one-is-not-persistent"),
        param("auto", TypeError, "'bastion' must be", id="string"),
        param([], TypeError, "'bastion' must be", id="list"),
        param({"unknown": True}, ValueError, "Unknown bastion", id="unknown-key"),
        param({"dns_domains": "internal.example"}, TypeError, "'dns_domains'", id="bare-domain"),
        param({"dns_domains": [7]}, TypeError, "DNS names must be strings", id="number-domain"),
        param({"dns_domains": ["bad..example"]}, ValueError, "Invalid DNS name", id="empty-label"),
        param(
            {"dns_domains": ["internal.example.."]},
            ValueError,
            "Invalid DNS name",
            id="two-final-dots",
        ),
        param({"dns_domains": ["*.example"]}, ValueError, "Invalid DNS name", id="wildcard"),
        param({"dns_domains": ["10.0.0.1"]}, ValueError, "must not be IP", id="ip-address"),
    ],
)
def test_invalid_bastion_policy_fails_at_vpc_construction(value, error, message):
    with raises(error, match=message):
        Vpc("net", bastion=value)


def test_persistent_bastion_customization_and_tags(network_state, pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        network = Vpc(
            "net",
            bastion=True,
            tags={"team": "platform"},
            customize={
                "bastion": {"instance_type": "t4g.micro"},
                "bastion_security_group": {"description": "Team dev access"},
                "bastion_identity_document": {"tags": {"owner": "team"}},
            },
        )
        return network.resources

    deploy()
    network = read_network_manifest(network_state).vpcs[0]
    assert network.access is not None
    instance = pulumi_mocks.assert_res(
        "net-bastion",
        R.EC2_INSTANCE,
        {
            "instanceType": "t4g.micro",
            "tags": {"team": "platform"},
            "volumeTags": {"team": "platform"},
        },
        partial=True,
    )
    assert instance.inputs["vpcSecurityGroupIds"] == [network.access.security_group_id]
    pulumi_mocks.assert_res(
        "net-bastion-sg",
        R.SECURITY_GROUP,
        {"description": "Team dev access", "tags": {"team": "platform"}},
        partial=True,
    )
    for name, typ in (
        ("net-bastion-role", R.ROLE),
        ("net-bastion-profile", R.INSTANCE_PROFILE),
        ("net-bastion-egress", R.SECURITY_GROUP_EGRESS_RULE),
    ):
        pulumi_mocks.assert_res(name, typ, {"tags": {"team": "platform"}}, partial=True)
    pulumi_mocks.assert_res(
        "net-bastion-identity",
        R.SSM_DOCUMENT,
        {"tags": {"owner": "team"}},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(
        {
            R.VPC: 1,
            R.INTERNET_GATEWAY: 1,
            R.DEFAULT_SECURITY_GROUP: 1,
            R.SUBNET: 6,
            R.ROUTE_TABLE: 6,
            R.ROUTE_TABLE_ASSOCIATION: 6,
            R.EC2_INSTANCE: 1,
            R.SECURITY_GROUP: 1,
            R.SECURITY_GROUP_EGRESS_RULE: 1,
            R.ROLE: 1,
            R.ROLE_POLICY: 1,
            R.INSTANCE_PROFILE: 1,
            R.SSM_DOCUMENT: 1,
        }
    )


def test_two_dev_endpoints_keep_exact_vpc_dependencies_and_bridge_ids(
    network_state,
    pulumi_mocks,
    project_cwd,
    monkeypatch,
):
    current = context()
    _ContextStore.clear()
    _ContextStore.set(replace(current, dev_mode=True))
    monkeypatch.setattr(
        "stelvio.aws.function.function.discover_or_create_appsync",
        lambda **kwargs: AppSyncResource(
            "api", "https://http.example", "wss://realtime.example", "test-key"
        ),
    )
    monkeypatch.setattr(
        "stelvio.aws.function.function._create_lambda_bridge_archive",
        lambda: pulumi.AssetArchive({"stub.py": pulumi.StringAsset("placeholder")}),
    )

    @pulumi.runtime.test
    def deploy():
        first = Vpc("first")

        def subnet(props):
            return {**props, "cidr_block": props["cidr_block"].replace("10.0.", "10.2.")}

        second = Vpc(
            "second",
            customize={
                "vpc": {"cidr_block": "10.2.0.0/16"},
                "public_subnet": subnet,
                "private_subnet": subnet,
                "isolated_subnet": subnet,
            },
        )
        return (
            Function("api0", handler="functions/simple.handler", vpc=first).resources,
            Function("api1", handler="functions/simple.handler", vpc=second).resources,
        )

    deploy()
    manifest = read_network_manifest(network_state)
    assert len(manifest.enabled_vpcs) == 2
    for index, name in enumerate(("first", "second")):
        endpoint = next(
            item for item in manifest.endpoints if item.identity.endswith(f"::api{index}")
        )
        network = next(item for item in manifest.vpcs if item.identity.endswith(f"::{name}"))
        assert endpoint.vpcs == (network.identity,)
        function = pulumi_mocks.assert_res(f"api{index}", R.FUNCTION)
        assert (
            endpoint.endpoint_id
            == function.inputs["environment"]["variables"]["STLV_DEV_ENDPOINT_ID"]
        )
        assert "vpcConfig" not in function.inputs
    pulumi_mocks.assert_res_counts(
        {
            R.VPC: 2,
            R.INTERNET_GATEWAY: 2,
            R.DEFAULT_SECURITY_GROUP: 2,
            R.SUBNET: 12,
            R.ROUTE_TABLE: 12,
            R.ROUTE_TABLE_ASSOCIATION: 12,
            R.SECURITY_GROUP: 2,
            R.SECURITY_GROUP_EGRESS_RULE: 2,
            R.FUNCTION: 2,
            R.ROLE: 2,
            R.ROLE_POLICY_ATTACHMENT: 4,
        }
    )
