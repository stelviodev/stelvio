"""Validate the minimal deployment records used to open local networking."""

from copy import deepcopy

from pytest import fixture, mark, param, raises

from stelvio.network import (
    OUTPUT_KEY,
    NetworkManifest,
    ResourceNetwork,
    VpcNetwork,
    normalize_dns_name,
)
from stelvio.stack_outputs import read_network_manifest

VPC_URN = "urn:pulumi:dev::sample::stelvio:aws:Vpc::net"
DATABASE_URN = "urn:pulumi:dev::sample::stelvio:aws:DocumentDb::database"


@fixture
def deployed_state():
    return {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": VPC_URN,
                        "type": "stelvio:aws:Vpc",
                        "outputs": {
                            OUTPUT_KEY: {
                                "version": 1,
                                "account": "123456789012",
                                "region": "us-east-1",
                                "vpc_id": "vpc-main",
                                "provider": "urn:pulumi:dev::sample::pulumi:providers:aws::main",
                                "route_subnet_ids": ["subnet-private", "subnet-isolated"],
                                "bastion_instance_id": "i-bastion",
                            }
                        },
                    },
                    {
                        "urn": DATABASE_URN,
                        "type": "stelvio:aws:DocumentDb",
                        "outputs": {
                            OUTPUT_KEY: {
                                "version": 1,
                                "vpc": VPC_URN,
                                "cluster_id": "database-cluster",
                            }
                        },
                    },
                ]
            }
        }
    }


def _entries(state):
    return state["checkpoint"]["latest"]["resources"]


def _metadata(state, index=0):
    return _entries(state)[index]["outputs"][OUTPUT_KEY]


def _add_second_vpc(state):
    second = deepcopy(_entries(state)[0])
    second["urn"] = second["urn"].replace("::net", "::second")
    second["outputs"][OUTPUT_KEY].update(
        vpc_id="vpc-second",
        route_subnet_ids=["subnet-second"],
        bastion_instance_id="i-second",
    )
    _entries(state).append(second)
    return second["outputs"][OUTPUT_KEY]


def test_state_reader_uses_component_identity_and_type(deployed_state):
    assert read_network_manifest(deployed_state) == NetworkManifest(
        vpcs=(
            VpcNetwork(
                identity=VPC_URN,
                account="123456789012",
                region="us-east-1",
                vpc_id="vpc-main",
                provider="urn:pulumi:dev::sample::pulumi:providers:aws::main",
                route_subnet_ids=("subnet-private", "subnet-isolated"),
                bastion_instance_id="i-bastion",
            ),
        ),
        resources=(ResourceNetwork(DATABASE_URN, VPC_URN, "database-cluster"),),
    )


def test_every_bastion_is_enabled_without_functions(deployed_state):
    _add_second_vpc(deployed_state)
    manifest = read_network_manifest(deployed_state)
    assert tuple(vpc.identity for vpc in manifest.enabled_vpcs) == (
        VPC_URN,
        VPC_URN.replace("::net", "::second"),
    )


def test_vpc_without_bastion_is_not_enabled(deployed_state):
    _metadata(deployed_state)["bastion_instance_id"] = None
    manifest = read_network_manifest(deployed_state)
    assert manifest.vpcs[0].bastion_instance_id is None
    assert manifest.enabled_vpcs == ()


def test_omitted_bastion_id_means_disabled_after_pulumi_serialization(deployed_state):
    del _metadata(deployed_state)["bastion_instance_id"]
    manifest = read_network_manifest(deployed_state)
    assert manifest.vpcs[0].bastion_instance_id is None
    assert manifest.enabled_vpcs == ()


def test_vpc_with_no_route_subnets_is_valid(deployed_state):
    _metadata(deployed_state)["route_subnet_ids"] = []
    assert read_network_manifest(deployed_state).vpcs[0].route_subnet_ids == ()


def test_function_requires_no_network_record(deployed_state):
    _entries(deployed_state).append(
        {"urn": "urn:function", "type": "stelvio:aws:Function", "outputs": {}}
    )
    assert len(read_network_manifest(deployed_state).vpcs) == 1


@mark.parametrize("state", [None, {}, {"checkpoint": {"latest": {"resources": []}}}])
def test_app_without_network_components_requires_no_records(state):
    assert read_network_manifest(state) == NetworkManifest((), ())


def test_state_reader_accepts_unwrapped_checkpoint(deployed_state):
    assert read_network_manifest(deployed_state["checkpoint"]) == read_network_manifest(
        deployed_state
    )


@mark.parametrize(
    ("index", "field", "value", "message"),
    [
        param(0, "account", "bad", "12-digit AWS account", id="account"),
        param(0, "account", 123456789012, "nonempty string", id="numeric-account"),
        param(0, "region", "", "nonempty string", id="empty-region"),
        param(0, "region", "us east 1", "nonempty string", id="region-whitespace"),
        param(0, "vpc_id", None, "nonempty string", id="missing-vpc-id"),
        param(0, "provider", "", "nonempty string", id="empty-provider"),
        param(0, "route_subnet_ids", "subnet-first", "list", id="subnet-list"),
        param(0, "route_subnet_ids", [""], "nonempty string", id="empty-subnet"),
        param(0, "route_subnet_ids", [True], "nonempty string", id="boolean-subnet"),
        param(
            0,
            "route_subnet_ids",
            ["subnet-first", "subnet-first"],
            "duplicate subnet IDs",
            id="duplicate-subnet",
        ),
        param(0, "bastion_instance_id", "", "nonempty string", id="empty-bastion"),
        param(0, "bastion_instance_id", False, "nonempty string", id="boolean-bastion"),
        param(1, "vpc", "", "nonempty string", id="empty-vpc-reference"),
        param(1, "vpc", "unknown", "unknown VPC", id="unknown-vpc-reference"),
        param(1, "cluster_id", "", "nonempty string", id="empty-cluster"),
    ],
)
def test_bad_record_field_names_component_field_and_fix(
    deployed_state, index, field, value, message
):
    _metadata(deployed_state, index)[field] = value
    with raises(ValueError, match=message) as failure:
        read_network_manifest(deployed_state)
    assert _entries(deployed_state)[index]["urn"] in str(failure.value)
    assert field in str(failure.value)
    assert "redeploy the app" in str(failure.value)


@mark.parametrize(("index", "field"), [(0, "vpc_id"), (1, "cluster_id"), (0, "version")])
def test_missing_record_field_names_component_field_and_fix(deployed_state, index, field):
    del _metadata(deployed_state, index)[field]
    with raises(ValueError, match="Invalid network metadata") as failure:
        read_network_manifest(deployed_state)
    assert _entries(deployed_state)[index]["urn"] in str(failure.value)
    assert field in str(failure.value)
    assert "redeploy the app" in str(failure.value)


def test_unknown_fields_are_named_without_exposing_values(deployed_state):
    _metadata(deployed_state)["unexpected"] = "must-never-appear"
    with raises(ValueError, match=r"unknown fields.*unexpected") as failure:
        read_network_manifest(deployed_state)
    assert VPC_URN in str(failure.value)
    assert "must-never-appear" not in str(failure.value)
    assert "redeploy the app" in str(failure.value)


@mark.parametrize("index", [0, 1], ids=["vpc", "documentdb"])
def test_missing_component_record_requests_redeploy(deployed_state, index):
    _entries(deployed_state)[index]["outputs"] = {}
    with raises(ValueError, match="Missing network metadata") as failure:
        read_network_manifest(deployed_state)
    assert _entries(deployed_state)[index]["urn"] in str(failure.value)
    assert OUTPUT_KEY in str(failure.value)
    assert "redeploy the app" in str(failure.value)


def test_record_must_be_an_object(deployed_state):
    _entries(deployed_state)[0]["outputs"][OUTPUT_KEY] = []
    with raises(ValueError, match="require a record object") as failure:
        read_network_manifest(deployed_state)
    assert VPC_URN in str(failure.value)
    assert OUTPUT_KEY in str(failure.value)


@mark.parametrize("version", [0, -1, True, "1", None, 0.5, 1.5])
def test_invalid_or_older_record_version_requests_redeploy(deployed_state, version):
    _metadata(deployed_state)["version"] = version
    with raises(ValueError, match="field 'version'") as failure:
        read_network_manifest(deployed_state)
    assert VPC_URN in str(failure.value)
    assert "redeploy the app" in str(failure.value)


def test_newer_record_version_requests_cli_upgrade(deployed_state):
    _metadata(deployed_state)["version"] = 2
    with raises(ValueError, match="field 'version'") as failure:
        read_network_manifest(deployed_state)
    assert VPC_URN in str(failure.value)
    assert "upgrade Stelvio" in str(failure.value)


def test_version_survives_pulumi_rpc_number_encoding(deployed_state):
    _metadata(deployed_state)["version"] = 1.0
    assert len(read_network_manifest(deployed_state).vpcs) == 1


def test_duplicate_component_identity_is_rejected(deployed_state):
    _entries(deployed_state).append(deepcopy(_entries(deployed_state)[0]))
    with raises(ValueError, match="duplicate component URN") as failure:
        read_network_manifest(deployed_state)
    assert VPC_URN in str(failure.value)


@mark.parametrize("field", ["vpc_id", "route_subnet_ids", "bastion_instance_id"])
def test_aws_network_resource_cannot_have_two_vpc_owners(deployed_state, field):
    second = _add_second_vpc(deployed_state)
    second[field] = _metadata(deployed_state)[field]
    with raises(ValueError, match="also claimed") as failure:
        read_network_manifest(deployed_state)
    assert VPC_URN in str(failure.value)
    assert "::second" in str(failure.value)
    assert field in str(failure.value)


@mark.parametrize(("field", "value"), [("account", "987654321012"), ("region", "eu-west-1")])
def test_physical_ids_are_scoped_to_aws_account_and_region(deployed_state, field, value):
    second = _add_second_vpc(deployed_state)
    second.update(_metadata(deployed_state), **{field: value})
    assert len(read_network_manifest(deployed_state).enabled_vpcs) == 2


def test_cluster_cannot_be_claimed_by_two_components(deployed_state):
    second = deepcopy(_entries(deployed_state)[1])
    second["urn"] += "-second"
    _entries(deployed_state).append(second)
    with raises(ValueError, match=r"field 'cluster_id'.*also claimed") as failure:
        read_network_manifest(deployed_state)
    assert DATABASE_URN in str(failure.value)


@mark.parametrize(
    ("value", "expected"),
    [
        ("Cluster.Internal.Example.", "cluster.internal.example"),
        ("bücher.example", "xn--bcher-kva.example"),
        ("internal", "internal"),
    ],
)
def test_dns_names_are_normalized(value, expected):
    assert normalize_dns_name(value) == expected


@mark.parametrize("value", ["", "*.example", "a..example", "with space.example", "127.0.0.1"])
def test_invalid_dns_names_name_the_bad_value(value):
    with raises(ValueError, match="Invalid DNS name") as failure:
        normalize_dns_name(value)
    assert repr(value) in str(failure.value)


def test_dns_name_type_error_names_the_bad_value():
    with raises(TypeError, match="got 42"):
        normalize_dns_name(42)
