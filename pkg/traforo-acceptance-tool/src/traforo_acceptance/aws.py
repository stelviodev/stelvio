"""Independent bounded AWS inventory. No candidate assertion imports."""

from __future__ import annotations

import re
import time

from botocore.config import Config
from botocore.exceptions import ClientError

IO = Config(connect_timeout=2, read_timeout=6, retries={"total_max_attempts": 1})


class Reader:
    def __init__(self, sdk, seconds=300, calls=128):
        self.sdk = sdk
        self.deadline = time.monotonic() + seconds
        self.remaining = calls
        self.clients = {}

    def call(self, service, operation, **kwargs):
        if time.monotonic() + 8.5 > self.deadline or self.remaining <= 0:
            raise TimeoutError("AWS inventory deadline/request budget exhausted")
        self.remaining -= 1
        if service not in self.clients:
            self.clients[service] = self.sdk.client(service, config=IO)
        return getattr(self.clients[service], operation)(**kwargs)

    def pages(self, service, operation, key, **kwargs):
        # Explicit pagination keeps SDK paginators from bypassing the call budget.
        token = None
        seen = set()
        for _ in range(32):
            response = self.call(service, operation, **kwargs)
            yield from response.get(key, [])
            token_key = next(
                (
                    k
                    for k in ("NextToken", "nextToken", "Marker", "NextMarker", "position")
                    if response.get(k)
                ),
                None,
            )
            if not token_key:
                if response.get("IsTruncated"):
                    raise RuntimeError(f"Missing pagination marker in {operation}")
                return
            token = response[token_key]
            if token in seen:
                raise RuntimeError(f"Repeated pagination marker in {operation}")
            seen.add(token)
            request_key = "Marker" if token_key == "NextMarker" else token_key
            kwargs[request_key] = token
        raise RuntimeError(f"Page budget exhausted in {operation}")


def validate_proof(proof):
    if not re.fullmatch(r"stlv-(?:tunnel-|t-)[0-9a-f]{8}", proof["app"]):
        raise RuntimeError("Unsupported proof namespace; refusing ownership inference")
    if proof["env"] not in {"proof", "tunnel-proof"}:
        raise RuntimeError("Unsupported proof environment")
    if not re.fullmatch(r"\d{12}", proof["account"]):
        raise RuntimeError("Invalid proof account")


def capture(sdk, proof, previous=()):
    validate_proof(proof)
    reader = Reader(sdk)
    if reader.call("sts", "get_caller_identity")["Account"] != proof["account"]:
        raise RuntimeError("AWS identity changed; refusing inventory")
    app, env = proof["app"], proof["env"]
    prefix = f"{app}-{env}-"
    filters = [
        {"Name": "tag:stelvio:app", "Values": [app]},
        {"Name": "tag:stelvio:env", "Values": [env]},
    ]
    remaining = {}
    endpoint_records = {}
    instances, volumes, secrets, functions = set(), set(), set(), set()
    for reservation in reader.pages("ec2", "describe_instances", "Reservations", Filters=filters):
        for instance in reservation["Instances"]:
            instances.add(instance["InstanceId"])
            volumes.update(
                x["Ebs"]["VolumeId"] for x in instance.get("BlockDeviceMappings", []) if "Ebs" in x
            )
            if instance["State"]["Name"] != "terminated":
                remaining.setdefault("instances", []).append(instance["InstanceId"])
    for operation, key, identifier in (
        ("describe_vpcs", "Vpcs", "VpcId"),
        ("describe_subnets", "Subnets", "SubnetId"),
        ("describe_route_tables", "RouteTables", "RouteTableId"),
        ("describe_internet_gateways", "InternetGateways", "InternetGatewayId"),
        ("describe_security_groups", "SecurityGroups", "GroupId"),
        ("describe_network_interfaces", "NetworkInterfaces", "NetworkInterfaceId"),
        ("describe_vpc_endpoints", "VpcEndpoints", "VpcEndpointId"),
        ("describe_nat_gateways", "NatGateways", "NatGatewayId"),
        ("describe_volumes", "Volumes", "VolumeId"),
        ("describe_addresses", "Addresses", "AllocationId"),
    ):
        parameters = {"Filter" if operation == "describe_nat_gateways" else "Filters": filters}
        rows = list(reader.pages("ec2", operation, key, **parameters))
        if operation == "describe_vpc_endpoints" and proof.get("suite") == "lambda-vpc":
            for row in rows:
                endpoint_records[row["VpcEndpointId"]] = {
                    "id": row["VpcEndpointId"],
                    "vpc": row["VpcId"],
                    "interfaces": row.get("NetworkInterfaceIds", []),
                }
        ids = [row[identifier] for row in rows if row.get("State") != "deleted"]
        if ids:
            remaining[key] = ids
    for operation, key, field in (
        ()
        if proof.get("suite") == "lambda-vpc"
        else (
            ("describe_db_clusters", "DBClusters", "DBClusterIdentifier"),
            ("describe_db_instances", "DBInstances", "DBInstanceIdentifier"),
            ("describe_db_subnet_groups", "DBSubnetGroups", "DBSubnetGroupName"),
        )
    ):
        for row in reader.pages("docdb", operation, key):
            if row[field].startswith(prefix):
                remaining.setdefault(key, []).append(row[field])
                if row.get("MasterUserSecret", {}).get("SecretArn"):
                    secrets.add(row["MasterUserSecret"]["SecretArn"])
    for row in reader.pages("lambda", "list_functions", "Functions"):
        if row["FunctionName"].startswith(prefix):
            functions.add(row["FunctionName"])
            remaining.setdefault("functions", []).append(row["FunctionName"])
    for row in reader.pages(
        "logs", "describe_log_groups", "logGroups", logGroupNamePrefix=f"/aws/lambda/{prefix}"
    ):
        remaining.setdefault("log_groups", []).append(row["logGroupName"])
    for operation, key, field in (
        ("list_roles", "Roles", "RoleName"),
        ("list_instance_profiles", "InstanceProfiles", "InstanceProfileName"),
        ("list_policies", "Policies", "PolicyName"),
    ):
        parameters = {"Scope": "Local"} if operation == "list_policies" else {}
        for row in reader.pages("iam", operation, key, **parameters):
            if row[field].startswith(prefix):
                remaining.setdefault(key, []).append(row[field])
    for row in reader.pages(
        "ssm",
        "list_documents",
        "DocumentIdentifiers",
        Filters=[{"Key": "Owner", "Values": ["Self"]}],
    ):
        if row["Name"].startswith(prefix):
            remaining.setdefault("documents", []).append(row["Name"])
    for prior in previous:
        instances.update(prior.get("instances", []))
        volumes.update(prior.get("volumes", []))
        secrets.update(prior.get("secrets", []))
        functions.update(prior.get("functions", []))
    for volume in sorted(volumes):
        try:
            rows = reader.call("ec2", "describe_volumes", VolumeIds=[volume]).get("Volumes", [])
        except ClientError as error:
            if error.response["Error"]["Code"] != "InvalidVolume.NotFound":
                raise
        else:
            if rows:
                remaining.setdefault("captured_volumes", []).append(volume)
    for secret in sorted(secrets):
        try:
            value = reader.call("secretsmanager", "describe_secret", SecretId=secret)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
        else:
            if not value.get("DeletedDate"):
                remaining.setdefault("secrets", []).append(secret)
    if proof.get("suite") == "lambda-vpc":
        for prior in previous:
            for row in prior.get("endpoint_records", []):
                known = endpoint_records.setdefault(row["id"], dict(row))
                if known.get("vpc") != row.get("vpc"):
                    raise RuntimeError("Endpoint VPC identity changed")
                known["interfaces"] = sorted(
                    set(known["interfaces"]) | set(row.get("interfaces", []))
                )
        for identifier, endpoint in sorted(endpoint_records.items()):
            try:
                rows = reader.call(
                    "ec2", "describe_vpc_endpoints", VpcEndpointIds=[identifier]
                ).get("VpcEndpoints", [])
            except ClientError as error:
                if error.response["Error"]["Code"] != "InvalidVpcEndpointId.NotFound":
                    raise
                rows = []
            endpoint["outcome"] = "absent"
            for row in rows:
                if row["VpcEndpointId"] != identifier or row["VpcId"] != endpoint["vpc"]:
                    raise RuntimeError("Captured endpoint identity changed")
                endpoint["interfaces"] = sorted(
                    set(endpoint["interfaces"]) | set(row.get("NetworkInterfaceIds", []))
                )
                if row.get("State") != "deleted":
                    endpoint["outcome"] = "remaining"
                    remaining.setdefault("captured_endpoints", []).append(identifier)
            endpoint["remaining_interfaces"] = []
            for interface in endpoint["interfaces"]:
                try:
                    rows = reader.call(
                        "ec2", "describe_network_interfaces", NetworkInterfaceIds=[interface]
                    ).get("NetworkInterfaces", [])
                except ClientError as error:
                    if error.response["Error"]["Code"] != "InvalidNetworkInterfaceID.NotFound":
                        raise
                    rows = []
                for row in rows:
                    if row["NetworkInterfaceId"] != interface or row["VpcId"] != endpoint["vpc"]:
                        raise RuntimeError("Captured interface identity changed")
                    endpoint["remaining_interfaces"].append(interface)
                    remaining.setdefault("captured_interfaces", []).append(interface)
    history = []
    for instance in sorted(instances):
        for state in ("Active", "History"):
            for row in reader.pages(
                "ssm",
                "describe_sessions",
                "Sessions",
                State=state,
                Filters=[{"key": "Target", "value": instance}],
            ):
                item = {k: str(row.get(k, "")) for k in ("SessionId", "Status", "Target", "Reason")}
                if state == "Active":
                    remaining.setdefault("active_ssm", []).append(item)
                else:
                    history.append(item)
    return {
        "remaining": remaining,
        "instances": sorted(instances),
        "volumes": sorted(volumes),
        "secrets": sorted(secrets),
        "functions": sorted(functions),
        "ssm_history": history,
        **(
            {"endpoint_records": list(endpoint_records.values())}
            if proof.get("suite") == "lambda-vpc"
            else {}
        ),
    }


def shared(sdk):
    return [
        {k: row[k] for k in ("apiId", "name", "apiArn")}
        for row in Reader(sdk).pages("appsync", "list_apis", "apis")
    ]
