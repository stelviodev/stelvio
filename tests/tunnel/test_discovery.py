"""Discovery follows actual cluster membership and commits only its own generation."""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from stelvio.network import NetworkManifest, ResourceNetwork, VpcNetwork
from stelvio.tunnel.discovery import DiscoveryError, DiscoveryPendingError, ResourceDiscovery


@pytest.fixture
def deployed():
    def vpc(name):
        return VpcNetwork(
            name,
            "123456789012",
            "us-east-1",
            "vpc-" + name,
            "provider-" + name,
            ("subnet-" + name,),
            "i-" + name,
        )

    return NetworkManifest(
        (vpc("a"), vpc("b")),
        (ResourceNetwork("db-a", "a", "cluster-a"), ResourceNetwork("db-b", "b", "cluster-b")),
    )


class Client:
    """Controlled AWS API boundary; production ownership and merge logic are exercised."""

    def __init__(self, owner):
        self.meta = SimpleNamespace(region_name="us-east-1")
        self.calls = []
        self.subnet = {"DBSubnetGroupName": "subnet-" + owner, "VpcId": "vpc-" + owner}
        self.cluster = {
            "DBClusterIdentifier": "cluster-" + owner,
            "Engine": "docdb",
            "Status": "available",
            "DBClusterArn": f"arn:aws:rds:us-east-1:123456789012:cluster:cluster-{owner}",
            "DBSubnetGroup": "subnet-" + owner,
            "Port": 27017,
            "Endpoint": f"cluster-{owner}.cluster-token.us-east-1.docdb.amazonaws.com",
            "ReaderEndpoint": f"cluster-{owner}.cluster-ro-token.us-east-1.docdb.amazonaws.com",
            "VpcSecurityGroups": [{"VpcSecurityGroupId": "sg-" + owner, "Status": "active"}],
            "DBClusterMembers": [
                {"DBInstanceIdentifier": "instance-" + owner, "IsClusterWriter": True}
            ],
        }
        self.member = {
            "DBInstanceIdentifier": "instance-" + owner,
            "DBClusterIdentifier": "cluster-" + owner,
            "Engine": "docdb",
            "DBInstanceStatus": "available",
            "DBInstanceArn": f"arn:aws:rds:us-east-1:123456789012:db:instance-{owner}",
            "DBSubnetGroup": deepcopy(self.subnet),
            "VpcSecurityGroups": deepcopy(self.cluster["VpcSecurityGroups"]),
            "Endpoint": {
                "Address": f"instance-{owner}.token.us-east-1.docdb.amazonaws.com",
                "Port": 27017,
            },
        }
        self.on_instance = lambda: None
        self.reads = 0
        self.changing = False

    def describe_db_clusters(self, **request):
        self.calls.append(request)
        self.reads += 1
        result = deepcopy(self.cluster)
        # AWS backup timestamps are unrelated to membership/readiness.
        result["LatestRestorableTime"] = self.reads
        if self.changing and self.reads > 1:
            result["DBClusterMembers"].append({"DBInstanceIdentifier": "new"})
        return {"DBClusters": [result]}

    def describe_db_subnet_groups(self, **request):
        self.calls.append(request)
        return {"DBSubnetGroups": [deepcopy(self.subnet)]}

    def describe_db_instances(self, **request):
        self.calls.append(request)
        self.on_instance()
        return {"DBInstances": [deepcopy(self.member)]}


def test_two_vpcs_get_exact_cluster_reader_and_member_names(deployed):
    discovery = ResourceDiscovery(deployed)
    for owner in ("a", "b"):
        discovery.begin(owner, 1)
        client = Client(owner)
        assert discovery.refresh(owner, 1, client)
        assert client.calls == [
            {"DBClusterIdentifier": "cluster-" + owner},
            {"DBSubnetGroupName": "subnet-" + owner},
            {"DBInstanceIdentifier": "instance-" + owner},
            {"DBClusterIdentifier": "cluster-" + owner},
        ]
    assert discovery.resources_for("a")[0].hostnames == (
        "cluster-a.cluster-token.us-east-1.docdb.amazonaws.com",
        "cluster-a.cluster-ro-token.us-east-1.docdb.amazonaws.com",
        "instance-a.token.us-east-1.docdb.amazonaws.com",
    )
    assert discovery.resources_for("b")[0].hostnames == (
        "cluster-b.cluster-token.us-east-1.docdb.amazonaws.com",
        "cluster-b.cluster-ro-token.us-east-1.docdb.amazonaws.com",
        "instance-b.token.us-east-1.docdb.amazonaws.com",
    )
    assert discovery.resources_for("a")[0].port == 27017


@pytest.mark.parametrize(
    ("target", "field", "value", "message"),
    [
        (
            "cluster",
            "DBClusterArn",
            "arn:aws:rds:us-east-1:999999999999:cluster:cluster-a",
            "account/region/identity",
        ),
        ("cluster", "Engine", "aurora", "identity or engine"),
        ("cluster", "Port", 27018, "member port differs"),
        ("subnet", "VpcId", "vpc-b", "another VPC"),
        ("member", "DBClusterIdentifier", "cluster-b", "another cluster"),
        (
            "member",
            "DBInstanceArn",
            "arn:aws:rds:eu-west-1:123456789012:db:instance-a",
            "account/region/identity",
        ),
        (
            "member",
            "DBSubnetGroup",
            {"DBSubnetGroupName": "subnet-a", "VpcId": "vpc-b"},
            "another VPC",
        ),
        (
            "member",
            "VpcSecurityGroups",
            [{"VpcSecurityGroupId": "sg-b", "Status": "active"}],
            "member security groups differ",
        ),
    ],
)
def test_foreign_or_changed_resource_never_commits_partial_discovery(
    deployed, target, field, value, message
):
    discovery = ResourceDiscovery(deployed)
    discovery.begin("a", 1)
    client = Client("a")
    getattr(client, target)[field] = value
    with pytest.raises(DiscoveryError, match=message):
        discovery.refresh("a", 1, client)
    assert discovery.resources_for("a") == ()


@pytest.mark.parametrize("target", ["cluster", "member"])
def test_extra_security_group_never_commits_partial_discovery(deployed, target):
    discovery = ResourceDiscovery(deployed)
    discovery.begin("a", 1)
    client = Client("a")
    getattr(client, target)["VpcSecurityGroups"].append(
        {"VpcSecurityGroupId": "sg-unexpected", "Status": "active"}
    )

    with pytest.raises(DiscoveryError, match="member security groups differ"):
        discovery.refresh("a", 1, client)

    assert discovery.resources_for("a") == ()


def test_membership_change_is_retryable_and_preserves_other_vpc(deployed):
    discovery = ResourceDiscovery(deployed)
    discovery.begin("b", 1)
    discovery.refresh("b", 1, Client("b"))
    previous = discovery.resources_for("b")
    discovery.begin("a", 1)
    client = Client("a")
    client.changing = True
    with pytest.raises(DiscoveryPendingError, match="changed during discovery"):
        discovery.refresh("a", 1, client)
    assert discovery.resources_for("b") is previous
    assert discovery.resources_for("a") == ()


def test_superseded_generation_cannot_commit_late_sdk_result(deployed):
    discovery = ResourceDiscovery(deployed)
    discovery.begin("a", 1)
    client = Client("a")
    client.on_instance = lambda: discovery.begin("a", 2)
    assert not discovery.refresh("a", 1, client)
    assert discovery.resources_for("a") == ()
    assert not discovery.refresh("a", 1, Client("a"))
    assert discovery.refresh("a", 2, Client("a"))


def test_exact_discovered_hostname_cannot_belong_to_two_vpcs(deployed):
    discovery = ResourceDiscovery(deployed)
    discovery.begin("a", 1)
    assert discovery.refresh("a", 1, Client("a"))
    previous = discovery.resources_for("a")
    client = Client("b")
    client.cluster["Endpoint"] = previous[0].hostnames[0]
    discovery.begin("b", 1)
    with pytest.raises(ValueError, match="DNS ownership conflict"):
        discovery.refresh("b", 1, client)
    assert discovery.resources_for("a") is previous
    assert discovery.resources_for("b") == ()


def test_second_database_failure_rolls_back_entire_vpc_refresh(deployed):
    second = replace(deployed.resources[0], identity="db-second", cluster_id="cluster-second")
    deployed = replace(deployed, resources=(*deployed.resources, second))
    discovery = ResourceDiscovery(deployed)
    discovery.begin("b", 1)
    assert discovery.refresh("b", 1, Client("b"))
    previous = discovery.resources_for("b")
    client = Client("a")
    describe = client.describe_db_clusters

    def clusters(**request):
        if request["DBClusterIdentifier"] == "cluster-second":
            return {"DBClusters": []}
        return describe(**request)

    client.describe_db_clusters = clusters
    discovery.begin("a", 1)
    with pytest.raises(DiscoveryError, match="ambiguous or missing identity"):
        discovery.refresh("a", 1, client)
    assert client.calls == [
        {"DBClusterIdentifier": "cluster-a"},
        {"DBSubnetGroupName": "subnet-a"},
        {"DBInstanceIdentifier": "instance-a"},
        {"DBClusterIdentifier": "cluster-a"},
    ]
    assert discovery.resources_for("b") is previous
    assert discovery.resources_for("a") == ()


@pytest.mark.parametrize("port", [None, True, 0, 65536, 27017.0])
def test_invalid_aws_port_does_not_commit_discovery(deployed, port):
    discovery = ResourceDiscovery(deployed)
    discovery.begin("a", 1)
    client = Client("a")
    client.cluster["Port"] = port
    with pytest.raises(DiscoveryError, match="service port must be an integer"):
        discovery.refresh("a", 1, client)
    assert discovery.resources_for("a") == ()


def test_current_cluster_configuration_is_read_from_aws(deployed):
    discovery = ResourceDiscovery(deployed)
    discovery.begin("a", 1)
    client = Client("a")
    client.cluster["Port"] = client.member["Endpoint"]["Port"] = 27018
    client.cluster["Endpoint"] = "new-cluster.internal.example"
    groups = [{"VpcSecurityGroupId": "sg-new", "Status": "active"}]
    client.cluster["VpcSecurityGroups"] = deepcopy(groups)
    client.member["VpcSecurityGroups"] = deepcopy(groups)
    assert discovery.refresh("a", 1, client)
    resource = discovery.resources_for("a")[0]
    assert resource.record == deployed.resources[0]
    assert resource.port == 27018
    assert resource.hostnames == (
        "new-cluster.internal.example",
        "cluster-a.cluster-ro-token.us-east-1.docdb.amazonaws.com",
        "instance-a.token.us-east-1.docdb.amazonaws.com",
    )


@pytest.mark.parametrize("hostname", ["*.example", "127.0.0.1", "with whitespace"])
def test_invalid_aws_hostname_does_not_commit_discovery(deployed, hostname):
    discovery = ResourceDiscovery(deployed)
    discovery.begin("a", 1)
    client = Client("a")
    client.cluster["Endpoint"] = hostname
    with pytest.raises(DiscoveryError, match="Invalid DNS name"):
        discovery.refresh("a", 1, client)
    assert discovery.resources_for("a") == ()


def test_vpc_without_databases_requires_no_docdb_calls(deployed):
    manifest = replace(deployed, resources=())
    discovery = ResourceDiscovery(manifest)
    discovery.begin("a", 1)
    client = Client("a")
    assert discovery.refresh("a", 1, client)
    assert client.calls == []
    assert discovery.resources_for("a") == ()
