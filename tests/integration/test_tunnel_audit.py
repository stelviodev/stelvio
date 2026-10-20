"""Deterministic coverage for standalone acceptance resource cleanup audits."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from . import assert_tunnel, cli_session


class _Paginator:
    def __init__(self, client: _Client, operation: str) -> None:
        self.client = client
        self.operation = operation

    def paginate(self, **kwargs):
        self.client.calls.append((self.operation, kwargs))
        return self.client.pages.get(self.operation, [])


class _Client:
    meta = None

    def __init__(self, *, direct: dict | None = None, pages: dict | None = None) -> None:
        self.direct = direct or {}
        self.pages = pages or {}
        self.calls: list[tuple[str, dict]] = []

    def get_paginator(self, operation: str) -> _Paginator:
        return _Paginator(self, operation)

    def __getattr__(self, operation: str):
        if operation not in self.direct:
            raise AssertionError(f"Unexpected AWS operation: {operation}")

        def call(**kwargs):
            self.calls.append((operation, kwargs))
            result = self.direct[operation]
            return result(**kwargs) if callable(result) else result

        return call


class _Session:
    def __init__(self, clients: dict[str, _Client]) -> None:
        self.clients = clients

    def client(self, service: str, **_kwargs) -> _Client:
        return self.clients[service]


def _empty_session() -> _Session:
    ec2 = _Client(
        direct={
            operation: {field: []}
            for operation, field in (
                ("describe_vpcs", "Vpcs"),
                ("describe_security_groups", "SecurityGroups"),
                ("describe_addresses", "Addresses"),
                ("describe_subnets", "Subnets"),
                ("describe_route_tables", "RouteTables"),
                ("describe_internet_gateways", "InternetGateways"),
                ("describe_network_interfaces", "NetworkInterfaces"),
                ("describe_volumes", "Volumes"),
                ("describe_instances", "Reservations"),
            )
        },
        pages={"describe_nat_gateways": [{"NatGateways": []}]},
    )
    docdb = _Client(
        pages={
            operation: [{field: []}]
            for operation, field in (
                ("describe_db_clusters", "DBClusters"),
                ("describe_db_instances", "DBInstances"),
                ("describe_db_subnet_groups", "DBSubnetGroups"),
                ("describe_db_cluster_parameter_groups", "DBClusterParameterGroups"),
            )
        }
    )
    functions = _Client(pages={"list_functions": [{"Functions": []}]})
    iam = _Client(
        pages={
            operation: [{field: []}]
            for operation, field in (
                ("list_roles", "Roles"),
                ("list_instance_profiles", "InstanceProfiles"),
                ("list_policies", "Policies"),
            )
        }
    )
    ssm = _Client(pages={"list_documents": [{"DocumentIdentifiers": []}]})
    return _Session({"ec2": ec2, "docdb": docdb, "lambda": functions, "iam": iam, "ssm": ssm})


def test_audit_covers_application_and_bastion_supporting_resources():
    session = _empty_session()
    ec2 = session.clients["ec2"]
    for operation, field, key, value in (
        ("describe_vpcs", "Vpcs", "VpcId", "vpc-1"),
        ("describe_security_groups", "SecurityGroups", "GroupId", "sg-1"),
        ("describe_addresses", "Addresses", "AllocationId", "eipalloc-1"),
        ("describe_subnets", "Subnets", "SubnetId", "subnet-1"),
        ("describe_route_tables", "RouteTables", "RouteTableId", "rtb-1"),
        ("describe_internet_gateways", "InternetGateways", "InternetGatewayId", "igw-1"),
        (
            "describe_network_interfaces",
            "NetworkInterfaces",
            "NetworkInterfaceId",
            "eni-1",
        ),
        ("describe_volumes", "Volumes", "VolumeId", "vol-1"),
    ):
        ec2.direct[operation][field] = [{key: value}]
    ec2.direct["describe_instances"]["Reservations"] = [
        {
            "Instances": [
                {"InstanceId": "i-running", "State": {"Name": "running"}},
                {"InstanceId": "i-terminated", "State": {"Name": "terminated"}},
            ]
        }
    ]
    ec2.pages["describe_nat_gateways"] = [
        {"NatGateways": [{"NatGatewayId": "nat-1", "State": "available"}]}
    ]
    session.clients["docdb"].pages.update(
        {
            "describe_db_clusters": [
                {"DBClusters": [{"DBClusterIdentifier": "demo-test-cluster"}]}
            ],
            "describe_db_instances": [
                {"DBInstances": [{"DBInstanceIdentifier": "demo-test-instance"}]}
            ],
            "describe_db_subnet_groups": [
                {"DBSubnetGroups": [{"DBSubnetGroupName": "demo-test-subnets"}]}
            ],
            "describe_db_cluster_parameter_groups": [
                {"DBClusterParameterGroups": [{"DBClusterParameterGroupName": "demo-test-params"}]}
            ],
        }
    )
    session.clients["lambda"].pages["list_functions"] = [
        {"Functions": [{"FunctionName": "demo-test-function"}]}
    ]
    session.clients["iam"].pages.update(
        {
            "list_roles": [{"Roles": [{"RoleName": "demo-test-role"}]}],
            "list_instance_profiles": [
                {"InstanceProfiles": [{"InstanceProfileName": "demo-test-profile"}]}
            ],
            "list_policies": [{"Policies": [{"PolicyName": "demo-test-policy"}]}],
        }
    )
    session.clients["ssm"].pages["list_documents"] = [
        {"DocumentIdentifiers": [{"Name": "demo-test-identity"}]}
    ]

    with pytest.raises(AssertionError) as error:
        assert_tunnel.assert_application_absent(
            "demo",
            "test",
            sdk=session,
            options=assert_tunnel.AbsenceAuditOptions(timeout=0),
        )

    message = str(error.value)
    for resource in (
        "VPC vpc-1",
        "EC2 instance i-running",
        "NAT gateway nat-1",
        "DocumentDB DBClusterIdentifier demo-test-cluster",
        "DocumentDB DBInstanceIdentifier demo-test-instance",
        "DocumentDB DBSubnetGroupName demo-test-subnets",
        "DocumentDB DBClusterParameterGroupName demo-test-params",
        "Lambda function demo-test-function",
        "IAM RoleName demo-test-role",
        "IAM InstanceProfileName demo-test-profile",
        "IAM PolicyName demo-test-policy",
        "SSM document demo-test-identity",
    ):
        assert resource in message
    assert "i-terminated" not in message
    iam_policy_call = next(
        call for call in session.clients["iam"].calls if call[0] == "list_policies"
    )
    assert iam_policy_call[1] == {"Scope": "Local"}
    ssm_document_call = next(
        call for call in session.clients["ssm"].calls if call[0] == "list_documents"
    )
    assert ssm_document_call[1] == {"Filters": [{"Key": "Owner", "Values": ["Self"]}]}


def test_audit_accepts_terminated_instances_deleted_nat_and_filters_vpc_lookup():
    session = _empty_session()
    ec2 = session.clients["ec2"]
    ec2.direct["describe_instances"]["Reservations"] = [
        {"Instances": [{"InstanceId": "i-terminated", "State": {"Name": "terminated"}}]}
    ]
    ec2.pages["describe_nat_gateways"] = [
        {"NatGateways": [{"NatGatewayId": "nat-deleted", "State": "deleted"}]}
    ]

    assert_tunnel.assert_application_absent(
        "demo",
        "test",
        sdk=session,
        options=assert_tunnel.AbsenceAuditOptions(timeout=0),
    )

    vpc_call = next(call for call in ec2.calls if call[0] == "describe_vpcs")
    assert vpc_call[1] == {"Filters": [{"Name": "tag:stelvio:app", "Values": ["demo"]}]}
    assert all(
        "VpcIds" not in kwargs for operation, kwargs in ec2.calls if operation == "describe_vpcs"
    )


def test_captures_exact_bastion_volume_ids_before_destroy():
    session = _empty_session()
    ec2 = session.clients["ec2"]
    ec2.direct["describe_instances"] = lambda **kwargs: {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-bastion-2",
                        "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-root-2"}}],
                    },
                    {
                        "InstanceId": "i-bastion-1",
                        "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-root-1"}}],
                    },
                ]
            }
        ]
    }

    assert assert_tunnel.capture_bastion_volume_ids(
        sdk=session, instance_ids=["i-bastion-1", "i-bastion-2"]
    ) == ("vol-root-1", "vol-root-2")
    assert ec2.calls[-1] == (
        "describe_instances",
        {"InstanceIds": ["i-bastion-1", "i-bastion-2"]},
    )


def test_audit_finds_untagged_bastion_volume_by_captured_id():
    session = _empty_session()
    ec2 = session.clients["ec2"]

    def describe_volumes(**kwargs):
        if "Filters" in kwargs:
            return {"Volumes": []}  # The root volume is intentionally untagged.
        assert kwargs == {"VolumeIds": ["vol-unowned-by-tags"]}
        return {"Volumes": [{"VolumeId": "vol-unowned-by-tags", "State": "available"}]}

    ec2.direct["describe_volumes"] = describe_volumes

    with pytest.raises(AssertionError, match="volume vol-unowned-by-tags"):
        assert_tunnel.assert_application_absent(
            "demo",
            "test",
            sdk=session,
            options=assert_tunnel.AbsenceAuditOptions(
                volume_ids=("vol-unowned-by-tags",), timeout=0
            ),
        )

    explicit_lookup = [
        kwargs
        for operation, kwargs in ec2.calls
        if operation == "describe_volumes" and "VolumeIds" in kwargs
    ]
    assert explicit_lookup == [{"VolumeIds": ["vol-unowned-by-tags"]}]


def test_audit_accepts_captured_volume_reported_not_found():
    session = _empty_session()
    ec2 = session.clients["ec2"]

    def describe_volumes(**kwargs):
        if "Filters" in kwargs:
            return {"Volumes": []}
        raise ClientError(
            {"Error": {"Code": "InvalidVolume.NotFound", "Message": "not found"}},
            "DescribeVolumes",
        )

    ec2.direct["describe_volumes"] = describe_volumes
    assert_tunnel.assert_application_absent(
        "demo",
        "test",
        sdk=session,
        options=assert_tunnel.AbsenceAuditOptions(volume_ids=("vol-deleted",), timeout=0),
    )


def test_request_budget_rejects_slow_scans_before_an_unbounded_next_call(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(assert_tunnel, "monotonic", clock.monotonic)
    budget = assert_tunnel._RequestBudget(deadline=10)

    budget.before_call()
    clock.now = 1.6  # Less than one bounded connect/read window remains.
    with pytest.raises(TimeoutError, match="bounded time or request budget"):
        budget.before_call()


def test_bastion_capture_uses_single_attempt_io_timeouts_and_deadline_hook():
    class Events:
        def __init__(self) -> None:
            self.registered = []

        def register(self, name, handler) -> None:
            self.registered.append((name, handler))

    class Client(_Client):
        def __init__(self) -> None:
            super().__init__()
            self.meta = type("Meta", (), {"events": Events()})()

    class ConfiguredSession:
        def __init__(self) -> None:
            self.client_config = None
            self.aws_client = Client()

        def client(self, _service, *, config):
            self.client_config = config
            return self.aws_client

    source = ConfiguredSession()
    source.aws_client.direct["describe_instances"] = {
        "Reservations": [
            {
                "Instances": [
                    {
                        "InstanceId": "i-bastion",
                        "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-root"}}],
                    }
                ]
            }
        ]
    }
    volume_ids = assert_tunnel.capture_bastion_volume_ids(sdk=source, instance_ids=["i-bastion"])
    client = source.aws_client

    assert volume_ids == ("vol-root",)
    assert source.client_config.connect_timeout == assert_tunnel.AWS_CONNECT_TIMEOUT
    assert source.client_config.read_timeout == assert_tunnel.AWS_READ_TIMEOUT
    assert source.client_config.retries["total_max_attempts"] == 1
    assert client.meta.events.registered[0][0] == "before-call.ec2.*"


def test_destroy_still_runs_when_bastion_volume_capture_cannot_complete(monkeypatch):
    session = object.__new__(cli_session.StandaloneCliSession)
    session._deployed = True
    session.process = None
    session._outputs = {}
    session.app = "demo"
    session.environment_name = "test"
    session.aws = object()
    operations = []
    monkeypatch.setattr(
        cli_session,
        "NativeHelper",
        lambda: SimpleNamespace(
            inspect=lambda: SimpleNamespace(owned=False, uncertain=False, mutating=False, units=[])
        ),
    )
    session.command = lambda *args, **kwargs: operations.append((args, kwargs))
    session._assert_aws_resources_absent = lambda: operations.append("audit")

    with pytest.raises(RuntimeError, match="block-device absence could not be certified"):
        session.destroy()

    assert operations[0][0] == ("destroy", "test", "--yes")
    assert operations[1] == "audit"


def test_audit_retries_until_absence_is_observed(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(assert_tunnel, "monotonic", clock.monotonic)
    monkeypatch.setattr(assert_tunnel, "sleep", clock.sleep)
    inventory = iter([["VPC vpc-1"], ["VPC vpc-1"], []])
    budgets = []

    def scan(_app, _env, *, sdk, **_kwargs):
        budgets.append(sdk.budget)
        for _ in range(21):
            sdk.budget.before_call()
        return next(inventory)

    monkeypatch.setattr(
        assert_tunnel,
        "_application_resources_remaining",
        scan,
    )

    assert_tunnel.assert_application_absent(
        "demo",
        "test",
        sdk=object(),
        options=assert_tunnel.AbsenceAuditOptions(timeout=30, poll_interval=4),
    )

    assert clock.now == 8
    assert clock.sleeps == [4, 4]
    assert len(budgets) == 3
    assert all(budget.requests == 21 for budget in budgets)
    assert len({id(budget) for budget in budgets}) == 3


def test_audit_stops_after_bounded_retry_deadline(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(assert_tunnel, "monotonic", clock.monotonic)
    monkeypatch.setattr(assert_tunnel, "sleep", clock.sleep)
    monkeypatch.setattr(
        assert_tunnel,
        "_application_resources_remaining",
        lambda *_args, **_kwargs: ["SSM document demo-test-identity"],
    )

    with pytest.raises(AssertionError, match=r"after 10s.*SSM document demo-test-identity"):
        assert_tunnel.assert_application_absent(
            "demo",
            "test",
            sdk=object(),
            options=assert_tunnel.AbsenceAuditOptions(timeout=10, poll_interval=4),
        )

    assert clock.now == 10
    assert clock.sleeps == [4, 4, 2]


class _Clock:
    def __init__(self) -> None:
        self.now = 0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
