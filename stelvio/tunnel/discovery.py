"""Discover resource hosts through captured clients, never the handler environment."""

from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

from stelvio.network import normalize_dns_name
from stelvio.tunnel.runtime_models import DiscoveredDocumentDb, validate_dns_ownership

if TYPE_CHECKING:
    from botocore.client import BaseClient

    from stelvio.network import NetworkManifest, ResourceNetwork, VpcNetwork

MAX_MEMBERS = 64
MAX_PORT = 65535
ARN_PARTS = 6
CLUSTER_CONFIGURATION = (
    "DBClusterIdentifier",
    "Engine",
    "DBClusterArn",
    "Status",
    "Endpoint",
    "ReaderEndpoint",
    "Port",
    "DBSubnetGroup",
    "VpcSecurityGroups",
    "DBClusterMembers",
)


class DiscoveryError(RuntimeError):
    """Deployed resource ownership/configuration no longer matches this session."""


class DiscoveryPendingError(DiscoveryError):
    """The owning resource is temporarily unavailable; retry discovery."""


def _one(response: dict, field: str) -> dict:
    values = response.get(field, [])
    if len(values) != 1 or not isinstance(values[0], dict):
        raise DiscoveryError("Resource discovery returned an ambiguous or missing identity")
    return values[0]


def _arn(value: str, network: VpcNetwork, kind: str, identifier: str) -> None:
    parts = value.split(":", 5)
    if (
        len(parts) != ARN_PARTS
        or parts[0] != "arn"
        or parts[1] not in ("aws", "aws-cn", "aws-us-gov")
        or parts[2:5] != ["rds", network.region, network.account]
        or parts[5] != f"{kind}:{identifier}"
    ):
        raise DiscoveryError("DocumentDB resource has a different AWS account/region/identity")


def _groups(actual: list[dict]) -> frozenset[str]:
    groups = [group.get("VpcSecurityGroupId") for group in actual]
    if (
        not groups
        or any(not isinstance(group, str) or not group for group in groups)
        or len(set(groups)) != len(groups)
    ):
        raise DiscoveryError("DocumentDB security group inventory is invalid")
    if any(group.get("Status") != "active" for group in actual):
        raise DiscoveryPendingError("DocumentDB security groups are changing")
    return frozenset(groups)


def _port(value: object) -> int:
    if type(value) is not int or not 1 <= value <= MAX_PORT:
        raise DiscoveryError(
            f"DocumentDB service port must be an integer in 1..65535, got {value!r}"
        )
    return value


def _endpoint(address: object) -> str:
    try:
        return normalize_dns_name(address)
    except (TypeError, ValueError) as error:
        raise DiscoveryError(str(error)) from error


def discover_documentdb(
    network: VpcNetwork, resource: ResourceNetwork, client: BaseClient
) -> DiscoveredDocumentDb:
    """Refresh exact cluster/member names, verifying every member belongs here.

    Clients must be created in the isolated runtime from the captured provider.
    DNS addresses are resolved through the VPC later, never cached as hosts-file
    records. A caller commits this immutable result only for its live generation.
    """
    if resource.vpc != network.identity:
        raise DiscoveryError("Resource discovery requested for another VPC or service")
    if client.meta.region_name != network.region:
        raise DiscoveryError("DocumentDB client has a different AWS region")
    cluster = _one(
        client.describe_db_clusters(DBClusterIdentifier=resource.cluster_id), "DBClusters"
    )
    if (
        cluster.get("DBClusterIdentifier") != resource.cluster_id
        or cluster.get("Engine") != "docdb"
    ):
        raise DiscoveryError("DocumentDB cluster identity or engine changed")
    _arn(cluster.get("DBClusterArn", ""), network, "cluster", resource.cluster_id)
    if cluster.get("Status") != "available":
        raise DiscoveryPendingError("DocumentDB cluster is not available")
    groups = _groups(cluster.get("VpcSecurityGroups", []))
    port = _port(cluster.get("Port"))
    subnet = _one(
        client.describe_db_subnet_groups(DBSubnetGroupName=cluster["DBSubnetGroup"]),
        "DBSubnetGroups",
    )
    if (
        subnet.get("DBSubnetGroupName") != cluster["DBSubnetGroup"]
        or subnet.get("VpcId") != network.vpc_id
    ):
        raise DiscoveryError("DocumentDB cluster belongs to another VPC")
    hosts = [
        _endpoint(cluster.get("Endpoint")),
        _endpoint(cluster.get("ReaderEndpoint")),
    ]
    members = cluster.get("DBClusterMembers", [])
    ids = [member.get("DBInstanceIdentifier") for member in members]
    if not ids:
        raise DiscoveryPendingError("DocumentDB cluster has no available members")
    if (
        len(ids) > MAX_MEMBERS
        or any(not isinstance(name, str) or not name for name in ids)
        or len(set(ids)) != len(ids)
    ):
        raise DiscoveryError("DocumentDB member discovery is ambiguous or exceeds its bound")
    hosts.extend(
        _member_host(network, resource, client, identifier, subnet, port, groups)
        for identifier in ids
    )
    # Catch membership changes during this multi-call inventory. No partial set
    # may become ready; the next generation/refresh retries from the cluster.
    current = _one(
        client.describe_db_clusters(DBClusterIdentifier=resource.cluster_id), "DBClusters"
    )
    if any(current.get(key) != cluster.get(key) for key in CLUSTER_CONFIGURATION):
        raise DiscoveryPendingError("DocumentDB cluster changed during discovery")
    return DiscoveredDocumentDb(resource, port, tuple(dict.fromkeys(hosts)))


def _member_host(  # noqa: PLR0913  # verify membership against one cluster snapshot
    network: VpcNetwork,
    resource: ResourceNetwork,
    client: BaseClient,
    identifier: str,
    subnet: dict,
    port: int,
    groups: frozenset[str],
) -> str:
    instance = _one(client.describe_db_instances(DBInstanceIdentifier=identifier), "DBInstances")
    if (
        instance.get("DBInstanceIdentifier") != identifier
        or instance.get("DBClusterIdentifier") != resource.cluster_id
        or instance.get("Engine") != "docdb"
    ):
        raise DiscoveryError("Discovered DocumentDB member belongs to another cluster")
    _arn(instance.get("DBInstanceArn", ""), network, "db", identifier)
    if instance.get("DBInstanceStatus") != "available":
        raise DiscoveryPendingError("DocumentDB member is not available")
    member_subnet = instance.get("DBSubnetGroup", {})
    if (
        member_subnet.get("VpcId") != network.vpc_id
        or member_subnet.get("DBSubnetGroupName") != subnet["DBSubnetGroupName"]
    ):
        raise DiscoveryError("Discovered DocumentDB member belongs to another VPC")
    if _groups(instance.get("VpcSecurityGroups", [])) != groups:
        raise DiscoveryError("DocumentDB member security groups differ from its cluster")
    endpoint = instance.get("Endpoint", {})
    if _port(endpoint.get("Port")) != port:
        raise DiscoveryError("DocumentDB member port differs from its cluster")
    return _endpoint(endpoint.get("Address"))


class ResourceDiscovery:
    """Generation-fenced, atomic per-VPC discovery; another VPC remains intact."""

    def __init__(self, manifest: NetworkManifest) -> None:
        self._deployed = manifest
        self._resources: dict[str, tuple[DiscoveredDocumentDb, ...]] = {}
        self._generations: dict[str, int] = {}
        self._lock = Lock()

    def resources_for(self, vpc: str) -> tuple[DiscoveredDocumentDb, ...]:
        with self._lock:
            return self._resources.get(vpc, ())

    def begin(self, vpc: str, generation: int) -> None:
        if vpc not in {network.identity for network in self._deployed.enabled_vpcs}:
            raise DiscoveryError("Cannot discover an unknown or disabled VPC")
        with self._lock:
            if type(generation) is not int or generation <= self._generations.get(vpc, 0):
                raise DiscoveryError("Discovery generation is stale")
            self._generations[vpc] = generation

    def refresh(self, vpc: str, generation: int, client: BaseClient) -> bool:
        with self._lock:
            if self._generations.get(vpc) != generation:
                return False
        network = next(
            network for network in self._deployed.enabled_vpcs if network.identity == vpc
        )
        updated = tuple(
            discover_documentdb(network, resource, client)
            for resource in self._deployed.resources
            if resource.vpc == vpc
        )
        with self._lock:
            if self._generations.get(vpc) != generation:
                return False
            candidate = self._resources | {vpc: updated}
            validate_dns_ownership(resource for group in candidate.values() for resource in group)
            self._resources = candidate
            return True
