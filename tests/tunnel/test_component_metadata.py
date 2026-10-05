"""Component outputs flow through the same state reader used by the dev CLI."""

import pulumi
from pulumi.runtime import rpc, settings
from pytest import fixture, mark, param, raises

from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import BastionConfig, Vpc
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
def test_vpc_policy_survives_deployment_state(value, policy, domains, network_state, pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return Vpc("net", **({} if value is ... else {"bastion": value})).resources

    deploy()
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
    pulumi_mocks.assert_res_counts(
        {
            R.VPC: 1,
            R.INTERNET_GATEWAY: 1,
            R.DEFAULT_SECURITY_GROUP: 1,
            R.SUBNET: 6,
            R.ROUTE_TABLE: 6,
            R.ROUTE_TABLE_ASSOCIATION: 6,
        }
    )


def test_function_and_documentdb_metadata_preserve_target_and_url(
    network_state,
    pulumi_mocks,
    project_cwd,
    mock_get_or_install_dependencies_function,
):
    @pulumi.runtime.test
    def deploy():
        net = Vpc("net")
        database = DocumentDb("db", vpc=net, customize={"cluster": {"port": 27018}})
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
    assert resource.security_groups == (tid("test-test-db-sg"),)
    assert endpoint.vpcs == (net.identity,)
    assert endpoint.endpoint_id.startswith("api-")
    outputs = format_outputs(group_outputs(network_state))
    assert any(
        f"https://{tid('test-test-api-url')}.lambda-url.us-east-1.on.aws/" in line
        for line in outputs
    )
    assert not any("documentdb" in line or "_stelvio_network" in line for line in outputs)
    pulumi_mocks.assert_res_counts(
        {
            R.VPC: 1,
            R.INTERNET_GATEWAY: 1,
            R.DEFAULT_SECURITY_GROUP: 1,
            R.SUBNET: 6,
            R.ROUTE_TABLE: 6,
            R.ROUTE_TABLE_ASSOCIATION: 6,
            R.SECURITY_GROUP: 2,
            R.SECURITY_GROUP_EGRESS_RULE: 1,
            R.SECURITY_GROUP_INGRESS_RULE: 1,
            R.DOCDB_CLUSTER: 1,
            R.DOCDB_INSTANCE: 1,
            R.DOCDB_SUBNET_GROUP: 1,
            R.DOCDB_PARAMETER_GROUP: 1,
            R.SECRET_ROTATION: 1,
            R.FUNCTION: 1,
            R.FUNCTION_URL: 1,
            R.ROLE: 1,
            R.POLICY: 1,
            R.ROLE_POLICY_ATTACHMENT: 3,
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
