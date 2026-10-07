"""Discover resource hosts through captured clients, never the handler environment."""

from __future__ import annotations

from dataclasses import replace
from threading import Lock
from typing import TYPE_CHECKING

from stelvio.tunnel.manifest import NetworkManifest, ResourceNetwork, validate_network_ownership
from stelvio.tunnel.policy import normalize_dns_name

if TYPE_CHECKING:
    from botocore.client import BaseClient

    from stelvio.tunnel.manifest import VpcNetwork

MAX_MEMBERS = 64
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


def _groups(actual: list[dict], expected: tuple[str, ...]) -> None:
    groups = {group["VpcSecurityGroupId"] for group in actual}
    if not expected or not set(expected).issubset(groups):
        raise DiscoveryError("DocumentDB security groups changed; redeploy before dev")
    if any(group.get("Status") != "active" for group in actual):
        raise DiscoveryPendingError("DocumentDB security groups are changing")


def _endpoint(address: object, port: object, resource: ResourceNetwork) -> str:
    if type(port) is not int or port not in resource.ports:
        raise DiscoveryError("DocumentDB service port changed; redeploy before dev")
    return normalize_dns_name(address)


def discover_documentdb(
    network: VpcNetwork, resource: ResourceNetwork, client: BaseClient
) -> ResourceNetwork:
    """Refresh exact cluster/member names, verifying every member belongs here.

    Clients must be created in the isolated runtime from the captured provider.
    DNS addresses are resolved through the VPC later, never cached as hosts-file
    records. A caller commits this immutable result only for its live generation.
    """
    if resource.vpc != network.identity or resource.service != "documentdb":
        raise DiscoveryError("Resource discovery requested for another VPC or service")
    if client.meta.region_name != network.region:
        raise DiscoveryError("DocumentDB client has a different AWS region")
    cluster = _one(
        client.describe_db_clusters(DBClusterIdentifier=resource.resource_id), "DBClusters"
    )
    if (
        cluster.get("DBClusterIdentifier") != resource.resource_id
        or cluster.get("Engine") != "docdb"
    ):
        raise DiscoveryError("DocumentDB cluster identity or engine changed")
    _arn(cluster.get("DBClusterArn", ""), network, "cluster", resource.resource_id)
    if cluster.get("Status") != "available":
        raise DiscoveryPendingError("DocumentDB cluster is not available")
    _groups(cluster.get("VpcSecurityGroups", []), resource.security_groups)
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
        _endpoint(cluster.get("Endpoint"), cluster.get("Port"), resource),
        _endpoint(cluster.get("ReaderEndpoint"), cluster.get("Port"), resource),
    ]
    if not set(resource.hostnames).issubset(hosts):
        raise DiscoveryError("Deployed DocumentDB endpoints changed; redeploy before dev")
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
    hosts.extend(_member_host(network, resource, client, identifier, subnet) for identifier in ids)
    # Catch membership changes during this multi-call inventory. No partial set
    # may become ready; the next generation/refresh retries from the cluster.
    current = _one(
        client.describe_db_clusters(DBClusterIdentifier=resource.resource_id), "DBClusters"
    )
    if any(current.get(key) != cluster.get(key) for key in CLUSTER_CONFIGURATION):
        raise DiscoveryPendingError("DocumentDB cluster changed during discovery")
    return replace(resource, hostnames=tuple(dict.fromkeys(hosts)))


def _member_host(
    network: VpcNetwork,
    resource: ResourceNetwork,
    client: BaseClient,
    identifier: str,
    subnet: dict,
) -> str:
    instance = _one(client.describe_db_instances(DBInstanceIdentifier=identifier), "DBInstances")
    if (
        instance.get("DBInstanceIdentifier") != identifier
        or instance.get("DBClusterIdentifier") != resource.resource_id
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
    _groups(instance.get("VpcSecurityGroups", []), resource.security_groups)
    endpoint = instance.get("Endpoint", {})
    return _endpoint(endpoint.get("Address"), endpoint.get("Port"), resource)


class ResourceDiscovery:
    """Generation-fenced, atomic per-VPC discovery; another VPC remains intact."""

    def __init__(self, manifest: NetworkManifest) -> None:
        self._deployed = manifest
        self._manifest = manifest
        self._generations: dict[str, int] = {}
        self._lock = Lock()

    @property
    def manifest(self) -> NetworkManifest:
        with self._lock:
            return self._manifest

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
        updated = {
            resource.identity: discover_documentdb(network, resource, client)
            for resource in self._deployed.resources
            if resource.vpc == vpc
        }
        with self._lock:
            if self._generations.get(vpc) != generation:
                return False
            candidate = replace(
                self._manifest,
                resources=tuple(
                    updated.get(resource.identity, resource)
                    for resource in self._manifest.resources
                ),
            )
            validate_network_ownership(candidate)
            self._manifest = candidate
            return True
