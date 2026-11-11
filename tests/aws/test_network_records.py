"""Component network records round-trip through deployed state."""

import pulumi
from pulumi.runtime import rpc, settings
from pytest import fixture, mark, param

from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc
from stelvio.network import OUTPUT_KEY
from stelvio.stack_outputs import format_outputs, group_outputs, read_network_manifest
from tests.aws.pulumi_mocks import ACCOUNT_ID, TP, R, tid

VPC_COUNTS = {
    R.VPC: 1,
    R.INTERNET_GATEWAY: 1,
    R.DEFAULT_SECURITY_GROUP: 1,
    R.SUBNET: 6,
    R.ROUTE_TABLE: 6,
    R.ROUTE_TABLE_ASSOCIATION: 6,
}
BASTION_COUNTS = {
    R.EC2_INSTANCE: 1,
    R.SECURITY_GROUP: 1,
    R.SECURITY_GROUP_EGRESS_RULE: 1,
    R.ROLE: 1,
    R.ROLE_POLICY: 1,
    R.INSTANCE_PROFILE: 1,
}
VPC_URN = "urn:pulumi:stack::project::pulumi:pulumi:Stack$stelvio:aws:Vpc::net"


@fixture
def network_state(pulumi_mocks, monkeypatch):
    monitor = settings.get_monitor()
    original = monitor.RegisterResourceOutputs
    resources = []

    def record(request):
        component_type = request.urn.split("::")[-2].split("$")[-1]
        if component_type.startswith("stelvio:"):
            resources.append(
                {
                    "urn": request.urn,
                    "type": component_type,
                    "outputs": rpc.deserialize_properties(request.outputs),
                }
            )
        return original(request)

    monkeypatch.setattr(monitor, "RegisterResourceOutputs", record)
    return {"checkpoint": {"latest": {"resources": resources}}}


@mark.parametrize(
    ("bastion", "instance_fields", "resource_counts"),
    [
        param(False, {}, VPC_COUNTS, id="off"),
        param(
            True,
            {"bastion_instance_id": tid(TP + "net-bastion")},
            VPC_COUNTS | BASTION_COUNTS,
            id="on",
        ),
    ],
)
def test_vpc_bastion_resources_and_record(
    bastion, instance_fields, resource_counts, network_state, pulumi_mocks
):
    @pulumi.runtime.test
    def deploy():
        return Vpc("net", bastion=bastion).resources

    deploy()

    assert network_state["checkpoint"]["latest"]["resources"] == [
        {
            "urn": VPC_URN,
            "type": "stelvio:aws:Vpc",
            "outputs": {
                OUTPUT_KEY: {
                    "version": 1,
                    "provider": (
                        "urn:pulumi:stack::project::pulumi:pulumi:Stack$"
                        "pulumi:providers:aws::stelvio-aws"
                    ),
                    "account": ACCOUNT_ID,
                    "region": "us-east-1",
                    "vpc_id": tid(TP + "net"),
                    "route_subnet_ids": [
                        tid(TP + "net-private-subnet-a"),
                        tid(TP + "net-private-subnet-b"),
                        tid(TP + "net-isolated-subnet-a"),
                        tid(TP + "net-isolated-subnet-b"),
                    ],
                    **instance_fields,
                }
            },
        }
    ]
    pulumi_mocks.assert_res_counts(resource_counts)


def test_document_db_record_contains_only_its_vpc_and_cluster(network_state):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb("db", vpc=Vpc("net")).resources

    deploy()

    records = network_state["checkpoint"]["latest"]["resources"]
    database = next(record for record in records if record["type"] == "stelvio:aws:DocumentDb")
    assert database["outputs"] == {
        OUTPUT_KEY: {"version": 1, "vpc": VPC_URN, "cluster_id": tid(TP + "db")}
    }
    resource = read_network_manifest(network_state).resources[0]
    assert (resource.identity, resource.vpc, resource.cluster_id) == (
        "urn:pulumi:stack::project::pulumi:pulumi:Stack$stelvio:aws:DocumentDb::db",
        VPC_URN,
        tid(TP + "db"),
    )


def test_every_bastion_vpc_is_enabled_without_functions(network_state):
    @pulumi.runtime.test
    def deploy():
        first = Vpc("net", bastion=True)
        return (
            DocumentDb("db", vpc=first).resources,
            Vpc("second", bastion=True).resources,
            Vpc("disabled").resources,
        )

    deploy()

    enabled = read_network_manifest(network_state).enabled_vpcs
    assert {network.identity for network in enabled} == {
        VPC_URN,
        "urn:pulumi:stack::project::pulumi:pulumi:Stack$stelvio:aws:Vpc::second",
    }


def test_private_network_records_are_hidden_from_display(network_state):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb("db", vpc=Vpc("net", bastion=True)).resources

    deploy()

    assert format_outputs(group_outputs(network_state)) == []


@mark.parametrize(
    ("url", "expected_outputs"),
    [
        param(None, {}, id="no-url"),
        param(
            "public",
            {"url": f"https://{tid(TP + 'api-url')}.lambda-url.us-east-1.on.aws/"},
            id="public-url",
        ),
    ],
)
def test_function_registers_only_public_outputs(
    url, expected_outputs, network_state, project_cwd, mock_get_or_install_dependencies_function
):
    @pulumi.runtime.test
    def deploy():
        return Function(
            "api", handler="functions/simple.handler", vpc=Vpc("net"), url=url
        ).resources

    deploy()

    records = network_state["checkpoint"]["latest"]["resources"]
    function = next(record for record in records if record["type"] == "stelvio:aws:Function")
    assert function["outputs"] == expected_outputs
    assert len(read_network_manifest(network_state).vpcs) == 1


@mark.parametrize(
    "membership",
    [
        param(lambda: ["sg-custom-a", "sg-custom-b"], id="custom-list"),
        param(lambda: ["sg-custom-b", "sg-custom-a"], id="reordered-list"),
        param(
            lambda: [pulumi.Output.from_input("sg-custom-a")],
            id="output-member",
        ),
        param(lambda: pulumi.Output.from_input(["sg-custom-a"]), id="output-list"),
    ],
)
def test_document_db_bastion_ingress_targets_its_owned_group(pulumi_mocks, membership):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb(
            "db",
            vpc=Vpc("net", bastion=True),
            tags={"team": "platform"},
            customize={"cluster": {"vpc_security_group_ids": membership(), "port": 27018}},
        ).resources

    deploy()

    rules = [
        resource
        for resource in pulumi_mocks.created_resources
        if resource.typ == R.SECURITY_GROUP_INGRESS_RULE
    ]
    assert {resource.name for resource in rules} == {TP + "db-ingress", TP + "db-bastion-ingress"}
    pulumi_mocks.assert_res(
        "db-bastion-ingress",
        R.SECURITY_GROUP_INGRESS_RULE,
        {
            "securityGroupId": tid(TP + "db-sg"),
            "referencedSecurityGroupId": tid(TP + "net-bastion-sg"),
            "ipProtocol": "tcp",
            "fromPort": 27018,
            "toPort": 27018,
            "tags": {"team": "platform"},
        },
    )


def test_document_db_bastion_ingress_exists_with_unknown_preview_membership(
    pulumi_mocks, monkeypatch
):
    monkeypatch.setattr(settings.SETTINGS, "dry_run", True)

    @pulumi.runtime.test
    def deploy():
        return DocumentDb(
            "db",
            vpc=Vpc("net", bastion=True),
            customize={
                "cluster": {"vpc_security_group_ids": pulumi.Output.from_input(pulumi.UNKNOWN)}
            },
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        "db-bastion-ingress",
        R.SECURITY_GROUP_INGRESS_RULE,
        {
            "securityGroupId": tid(TP + "db-sg"),
            "referencedSecurityGroupId": tid(TP + "net-bastion-sg"),
            "ipProtocol": "tcp",
            "fromPort": 27017,
            "toPort": 27017,
        },
    )


def test_document_db_has_only_app_ingress_without_bastion(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return DocumentDb("db", vpc=Vpc("net")).resources

    deploy()

    rules = [
        resource
        for resource in pulumi_mocks.created_resources
        if resource.typ == R.SECURITY_GROUP_INGRESS_RULE
    ]
    assert [resource.name for resource in rules] == [TP + "db-ingress"]
    pulumi_mocks.assert_res(
        "db-ingress",
        R.SECURITY_GROUP_INGRESS_RULE,
        {
            "securityGroupId": tid(TP + "db-sg"),
            "referencedSecurityGroupId": tid(TP + "net-app-sg"),
            "ipProtocol": "tcp",
            "fromPort": 27017,
            "toPort": 27017,
        },
    )


def test_bastion_resources_have_names_and_component_tags(pulumi_mocks):
    @pulumi.runtime.test
    def deploy():
        return Vpc("net", bastion=True, tags={"team": "platform"}).resources

    deploy()

    for name, typ, expected_tags in (
        ("net-bastion", R.EC2_INSTANCE, {"Name": TP + "net-bastion", "team": "platform"}),
        ("net-bastion-sg", R.SECURITY_GROUP, {"Name": TP + "net-bastion-sg", "team": "platform"}),
        ("net-bastion-role", R.ROLE, {"team": "platform"}),
        ("net-bastion-profile", R.INSTANCE_PROFILE, {"team": "platform"}),
        ("net-bastion-egress", R.SECURITY_GROUP_EGRESS_RULE, {"team": "platform"}),
    ):
        assert pulumi_mocks.assert_res(name, typ).inputs["tags"] == expected_tags
    instance = pulumi_mocks.assert_res("net-bastion", R.EC2_INSTANCE)
    assert instance.inputs["volumeTags"] == {"Name": TP + "net-bastion", "team": "platform"}
