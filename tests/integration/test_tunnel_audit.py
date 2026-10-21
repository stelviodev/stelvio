"""Deterministic coverage for standalone acceptance resource cleanup audits."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from . import assert_tunnel, cli_session


def _write_cli_session_project(tmp_path, *, no_vpc: bool = False) -> str:
    session = object.__new__(cli_session.StandaloneCliSession)
    session.project = tmp_path
    session.no_vpc = no_vpc
    session.app = "proof-app"
    session.profile = "proof-profile"
    session.region = "us-east-1"
    session.environment_name = "tunnel-proof"
    session.invocation_path = tmp_path / "imports.jsonl"
    session._write_project()
    source = (tmp_path / "stlv_app.py").read_text()
    compile(source, str(tmp_path / "stlv_app.py"), "exec")
    return source


def test_mixed_dev_proof_project_covers_enabled_disabled_and_plain_functions(tmp_path):
    source = _write_cli_session_project(tmp_path)

    assert "((1, 252, True), (2, 253, {}))" in source
    assert "bastion=False" in source
    assert "disabled-client" in source
    assert "plain_function = Function('plain'" in source
    assert "export_output('function3_url'" in source
    assert "export_output('function4_url'" in source


def test_non_vpc_proof_project_has_no_vpc_resource(tmp_path):
    source = _write_cli_session_project(tmp_path, no_vpc=True)

    assert "Function('plain', handler='handlers/plain.handler'" in source
    assert "Vpc(" not in source


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


def test_destroy_still_runs_when_bastion_volume_capture_cannot_complete(monkeypatch, tmp_path):
    session = object.__new__(cli_session.StandaloneCliSession)
    session._deployed = True
    session.no_vpc = False
    session.process = None
    session._outputs = {
        "bastion1_id": "i-bastion-1",
        "bastion2_id": "i-bastion-2",
    }
    session.app = "demo"
    session.environment_name = "test"
    session.aws = object()
    helper_path = tmp_path / "stelvio-traforo"
    helper_path.touch()
    monkeypatch.setattr(cli_session, "INSTALLED_HELPER", helper_path)

    def fail_volume_capture(**_kwargs):
        raise RuntimeError("volume inventory unavailable")

    monkeypatch.setattr(cli_session, "capture_proof_volume_ids", fail_volume_capture)
    operations = []
    monkeypatch.setattr(
        cli_session,
        "NativeHelper",
        lambda: SimpleNamespace(
            status=lambda: SimpleNamespace(owned=False, uncertain=False, mutating=False, units=[])
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


def _fault_session():
    node = {
        "InstanceId": "i-proof",
        "VpcId": "vpc-proof",
        "State": {"Name": "running"},
        "Tags": [{"Key": "stelvio:app", "Value": "demo"}, {"Key": "stelvio:env", "Value": "test"}],
    }
    reservation = {"OwnerId": "123456789012", "Instances": [node]}
    ec2 = _Client(direct={"describe_instances": {"Reservations": [reservation]}})
    ssm = _Client(
        direct={
            "send_command": {"Command": {"CommandId": "proof-command"}},
            "get_command_invocation": {
                "Status": "Success",
                "StandardOutputContent": "proof-output",
            },
        }
    )
    session = object.__new__(cli_session.StandaloneCliSession)
    session._manifest = SimpleNamespace(
        enabled_vpcs=(
            SimpleNamespace(access=SimpleNamespace(instance_id="i-proof"), vpc_id="vpc-proof"),
        )
    )
    session.aws = _Session({"ec2": ec2, "ssm": ssm})
    session.account = "123456789012"
    session.app = "demo"
    session.environment_name = "test"
    return session, node, reservation, ssm


@pytest.mark.parametrize("change", ["instance", "vpc", "tags", "account"])
def test_fault_command_refuses_foreign_or_changed_bastion(change):
    session, node, reservation, ssm = _fault_session()
    if change == "instance":
        target = "i-foreign"
    else:
        target = "i-proof"
        if change == "vpc":
            node["VpcId"] = "vpc-foreign"
        elif change == "tags":
            node["Tags"] = []
        else:
            reservation["OwnerId"] = "999999999999"
    with pytest.raises(RuntimeError, match=r"[Rr]efus"):
        session.run_ssm_commands(target, ["true"])
    assert not ssm.calls


def test_fault_command_sets_execution_timeout_and_clears_uncertainty_on_success():
    session, _, _, ssm = _fault_session()
    assert session.run_ssm_commands("i-proof", ["true"], timeout=30) == "proof-output"
    assert session._fault_command_uncertain is None
    operation, request = ssm.calls[0]
    assert operation == "send_command"
    assert request["Parameters"]["executionTimeout"] == ["30"]
    assert request["InstanceIds"] == ["i-proof"]
    assert request["DocumentName"] == "AWS-RunShellScript"


def test_uncertain_fault_command_blocks_later_commands(monkeypatch):
    session, _, _, ssm = _fault_session()
    clock = iter([0, 31])
    with monkeypatch.context() as patch:
        patch.setattr(cli_session.time, "monotonic", lambda: next(clock))
        with pytest.raises(TimeoutError):
            session.run_ssm_commands("i-proof", ["true"], timeout=30)
    assert session._fault_command_uncertain == "proof-command"
    with pytest.raises(RuntimeError, match="uncertain"):
        session.run_ssm_commands("i-proof", ["restore"])
    assert [operation for operation, _ in ssm.calls] == ["send_command"]


def test_host_key_fault_restores_on_body_failure_but_not_uncertain_setup():
    session, _, _, _ = _fault_session()
    calls = []
    session.run_ssm_commands = (
        lambda instance, commands: calls.append(commands) or "public fixture"
    )
    with pytest.raises(ValueError, match="proof failure"), session.rotated_host_key("i-proof"):
        raise ValueError("proof failure")
    assert len(calls) == 2
    assert any("cp -p" in command for command in calls[1])
    calls.clear()

    def uncertain(instance, commands):
        calls.append(commands)
        session._fault_command_uncertain = "proof-command"
        raise TimeoutError("response lost")

    session.run_ssm_commands = uncertain
    with pytest.raises(RuntimeError, match="do not race"), session.rotated_host_key("i-proof"):
        pytest.fail("uncertain setup succeeded")
    assert len(calls) == 1


@pytest.mark.parametrize("audit_fails", [False, True])
@pytest.mark.parametrize("helper_present", [False, True])
def test_destroy_resets_proof_pins_only_after_certified_aws_absence(
    monkeypatch, audit_fails, helper_present
):
    session = object.__new__(cli_session.StandaloneCliSession)
    session._deployed = True
    session.app = "demo"
    session.no_vpc = False
    session.environment_name = "test"
    session.process = None
    session._outputs = {"bastion1_id": "i-first", "bastion2_id": "i-second"}
    session.aws = object()
    operations = []
    session.command = lambda *args, **kwargs: operations.append("destroy")
    session._clear_proof_host_trust = lambda: operations.append("reset-proof-pins")

    def audit():
        operations.append("audit")
        if audit_fails:
            raise AssertionError("AWS absence is uncertain")

    session._assert_aws_resources_absent = audit
    monkeypatch.setattr(cli_session, "capture_proof_volume_ids", lambda **kwargs: ())
    monkeypatch.setattr(
        cli_session, "INSTALLED_HELPER", SimpleNamespace(exists=lambda: helper_present)
    )
    monkeypatch.setattr(
        cli_session,
        "NativeHelper",
        lambda: SimpleNamespace(
            status=lambda: SimpleNamespace(owned=False, uncertain=False, mutating=False, units=[])
        ),
    )
    if not helper_present:
        with pytest.raises(RuntimeError, match="helper is unavailable"):
            session.destroy()
        assert operations == []
    elif audit_fails:
        with pytest.raises(AssertionError, match="uncertain"):
            session.destroy()
        assert operations == ["destroy", "audit"]
    else:
        session.destroy()
        assert operations == ["destroy", "audit", "reset-proof-pins"]


@pytest.mark.parametrize(
    "fault",
    [None, "whole-vpc", "public", "resolver", "unused", "shared-interface", "missing-interface"],
)
def test_standalone_route_acceptance_requires_exact_subnets_and_exclusions(
    monkeypatch, tmp_path, fault
):
    from ipaddress import IPv4Address, IPv4Network

    session = object.__new__(cli_session.StandaloneCliSession)
    session.project = tmp_path
    networks = []
    for octet in (252, 253):
        public = SimpleNamespace(cidr=IPv4Network(f"10.{octet}.0.0/24"))
        private = SimpleNamespace(cidr=IPv4Network(f"10.{octet}.1.0/24"))
        isolated = SimpleNamespace(cidr=IPv4Network(f"10.{octet}.2.0/24"))
        networks.append(
            SimpleNamespace(
                cidrs=(IPv4Network(f"10.{octet}.0.0/16"),),
                route_cidrs=(private.cidr, isolated.cidr),
                public_subnets=(public,),
                subnets=(public, private, isolated),
            )
        )
    session._manifest = SimpleNamespace(enabled_vpcs=tuple(networks))
    queries = []

    def lookup(*args):
        assert args[:4] == ("/sbin/route", "-n", "get", "-inet")
        address = IPv4Address(args[4])
        queries.append(address)
        network = next(n for n in networks if address in n.cidrs[0])
        selected = next((c for c in network.route_cidrs if address in c), None)
        interface = "utun21" if address in networks[0].cidrs[0] else "utun22"
        if selected:
            destination = network.cidrs[0] if fault == "whole-vpc" else selected
            if fault == "shared-interface":
                interface = "utun21"
            return SimpleNamespace(
                stdout=f"destination: {destination.network_address}\nmask: {destination.netmask}\n"
                + ("" if fault == "missing-interface" else f"interface: {interface}\n")
            )
        bad = (
            (fault == "public" and address == network.cidrs[0].network_address + 1)
            or (fault == "resolver" and address == network.cidrs[0].network_address + 2)
            or (fault == "unused" and address == network.cidrs[0].broadcast_address - 1)
        )
        return SimpleNamespace(
            stdout=f"destination: default\ninterface: {interface if bad else 'en0'}\n"
        )

    monkeypatch.setattr("tests.integration.macos_host.run", lookup)
    if fault:
        with pytest.raises(AssertionError):
            session.assert_selected_routes()
    else:
        session.assert_selected_routes()
        assert len(queries) == 10
    assert (tmp_path / "standalone-routes.jsonl").read_text()


def _proof_instance(identifier, *, app="demo", env="test"):
    return {
        "InstanceId": identifier,
        "Tags": [{"Key": "stelvio:app", "Value": app}, {"Key": "stelvio:env", "Value": env}],
        "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-" + identifier}}],
    }


@pytest.mark.parametrize("known", [[], ["i-one"], ["i-one", "i-two"]])
def test_partial_deployment_discovers_all_proof_volumes_before_destroy(known):
    sdk = _empty_session()
    ec2 = sdk.clients["ec2"]
    first, second = _proof_instance("i-one"), _proof_instance("i-two")
    ec2.pages["describe_instances"] = [
        {"Reservations": [{"Instances": [first]}]},
        {"Reservations": [{"Instances": [second]}]},
    ]
    ec2.direct["describe_instances"] = {"Reservations": [{"Instances": [first, second]}]}

    assert assert_tunnel.capture_proof_volume_ids(
        sdk=sdk, app="demo", env="test", instance_ids=known
    ) == ("vol-i-one", "vol-i-two")
    assert ec2.calls == [
        (
            "describe_instances",
            {
                "Filters": [
                    {"Name": "tag:stelvio:app", "Values": ["demo"]},
                    {"Name": "tag:stelvio:env", "Values": ["test"]},
                ]
            },
        ),
        ("describe_instances", {"InstanceIds": ["i-one", "i-two"]}),
    ]


@pytest.mark.parametrize("known", [[], ["i-missing"]])
def test_empty_discovery_only_accepts_absence_without_known_bastions(known):
    sdk = _empty_session()
    sdk.clients["ec2"].pages["describe_instances"] = [{"Reservations": []}]
    if known:
        with pytest.raises(AssertionError, match="every known bastion"):
            assert_tunnel.capture_proof_volume_ids(
                sdk=sdk, app="demo", env="test", instance_ids=known
            )
    else:
        assert (
            assert_tunnel.capture_proof_volume_ids(
                sdk=sdk, app="demo", env="test", instance_ids=[]
            )
            == ()
        )
    assert len(sdk.clients["ec2"].calls) == 1


@pytest.mark.parametrize("change", [{"app": "foreign"}, {"env": "foreign"}])
def test_proof_volume_discovery_rejects_foreign_instances(change):
    sdk = _empty_session()
    sdk.clients["ec2"].pages["describe_instances"] = [
        {"Reservations": [{"Instances": [_proof_instance("i-one", **change)]}]}
    ]
    with pytest.raises(AssertionError, match="foreign"):
        assert_tunnel.capture_proof_volume_ids(sdk=sdk, app="demo", env="test", instance_ids=[])
    assert len(sdk.clients["ec2"].calls) == 1


def test_partial_discovery_failure_does_not_certify_empty_or_complete_inventory():
    def pages():
        yield {"Reservations": [{"Instances": [_proof_instance("i-one")]}]}
        raise RuntimeError("discovery interrupted")

    sdk = _empty_session()
    sdk.clients["ec2"].pages["describe_instances"] = pages()
    with pytest.raises(RuntimeError, match="discovery interrupted"):
        assert_tunnel.capture_proof_volume_ids(sdk=sdk, app="demo", env="test", instance_ids=[])
    assert len(sdk.clients["ec2"].calls) == 1


def test_proof_discovery_has_a_page_bound():
    sdk = _empty_session()
    sdk.clients["ec2"].pages["describe_instances"] = [
        {"Reservations": []} for _ in range(assert_tunnel.AWS_MAX_REQUESTS + 1)
    ]
    with pytest.raises(AssertionError, match="page budget"):
        assert_tunnel.capture_proof_volume_ids(sdk=sdk, app="demo", env="test", instance_ids=[])


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_deployment_preserves_original_and_any_cleanup_error(
    monkeypatch, tmp_path, cleanup_fails, interrupted
):
    from . import test_tunnel

    setup_error = KeyboardInterrupt() if interrupted else TypeError("invalid proof configuration")
    cleanup_error = RuntimeError("uncertified cleanup")
    events = []

    def deploy():
        events.append("deploy")
        raise setup_error

    def destroy():
        events.append("destroy")
        if cleanup_fails:
            raise cleanup_error

    monkeypatch.setattr(
        test_tunnel,
        "StandaloneCliSession",
        lambda _: SimpleNamespace(deploy=deploy, destroy=destroy),
    )
    fixture = test_tunnel.standalone_cli_session.__wrapped__(tmp_path)
    if cleanup_fails:
        with pytest.raises(BaseExceptionGroup) as caught:
            next(fixture)
        assert caught.value.exceptions == (setup_error, cleanup_error)
    else:
        with pytest.raises(type(setup_error)) as caught:
            next(fixture)
        assert caught.value is setup_error
    assert events == ["deploy", "destroy"]


@pytest.mark.parametrize("capture_fails", [False, True])
def test_partial_destroy_audits_after_command_failure_and_retains_all_errors(
    monkeypatch, tmp_path, capture_fails
):
    session = object.__new__(cli_session.StandaloneCliSession)
    session._deployed = True
    session.no_vpc = False
    session.process = None
    session._outputs = {}
    session.app = "demo"
    session.environment_name = "test"
    session.aws = _empty_session()
    session.aws.clients["ec2"].pages["describe_instances"] = [{"Reservations": []}]
    operations = []
    path = tmp_path / "helper"
    path.touch()
    monkeypatch.setattr(cli_session, "INSTALLED_HELPER", path)
    monkeypatch.setattr(
        cli_session,
        "NativeHelper",
        lambda: SimpleNamespace(
            status=lambda: SimpleNamespace(owned=False, uncertain=False, mutating=False, units=[])
        ),
    )
    capture_error = RuntimeError("inventory unavailable")
    destroy_error = RuntimeError("destroy unavailable")
    audit_error = AssertionError("resources remain")

    if capture_fails:
        monkeypatch.setattr(
            cli_session, "capture_proof_volume_ids", Mock(side_effect=capture_error)
        )

    def destroy(*args, **kwargs):
        operations.append("destroy")
        raise destroy_error

    def audit():
        operations.append("audit")
        raise audit_error

    session.command = destroy
    session._assert_aws_resources_absent = audit
    session._clear_proof_host_trust = lambda: operations.append("reset")
    with pytest.raises(ExceptionGroup) as caught:
        session.destroy()
    errors = caught.value.exceptions
    assert errors[-2:] == (destroy_error, audit_error)
    if capture_fails:
        assert errors[0].__cause__ is capture_error
    else:
        assert len(errors) == 2
    assert operations == ["destroy", "audit"]


def test_destroy_before_infrastructure_creation_needs_no_volume_capture(monkeypatch, tmp_path):
    session = object.__new__(cli_session.StandaloneCliSession)
    session._deployed = True
    session.no_vpc = False
    session.process = None
    session._outputs = {}
    session.app = "demo"
    session.environment_name = "test"
    session.aws = _empty_session()
    session.aws.clients["ec2"].pages["describe_instances"] = [{"Reservations": []}]
    helper = tmp_path / "helper"
    helper.touch()
    monkeypatch.setattr(cli_session, "INSTALLED_HELPER", helper)
    operations = []
    monkeypatch.setattr(
        cli_session,
        "NativeHelper",
        lambda: SimpleNamespace(
            status=lambda: SimpleNamespace(owned=False, uncertain=False, mutating=False, units=[])
        ),
    )
    capture = Mock(side_effect=AssertionError("empty capture called"))
    monkeypatch.setattr(assert_tunnel, "capture_bastion_volume_ids", capture)
    session.command = lambda *args, **kwargs: operations.append("destroy")
    session._assert_aws_resources_absent = lambda: operations.append("audit")
    session._clear_proof_host_trust = lambda: operations.append("reset")
    session.destroy()
    assert operations == ["destroy", "audit", "reset"]
    capture.assert_not_called()


def test_proof_discovery_rejects_duplicate_instances():
    sdk = _empty_session()
    sdk.clients["ec2"].pages["describe_instances"] = [
        {"Reservations": [{"Instances": [_proof_instance("i-one"), _proof_instance("i-one")]}]}
    ]
    with pytest.raises(AssertionError, match="duplicate"):
        assert_tunnel.capture_proof_volume_ids(sdk=sdk, app="demo", env="test", instance_ids=[])


@pytest.mark.parametrize(
    ("operation", "field", "identifier", "arn_key"),
    [
        ("describe_db_clusters", "DBClusters", "DBClusterIdentifier", "DBClusterArn"),
        ("describe_db_instances", "DBInstances", "DBInstanceIdentifier", "DBInstanceArn"),
    ],
)
@pytest.mark.parametrize(
    "tags",
    [
        {"stelvio:app": "demo", "stelvio:env": "test"},
        {"stelvio:app": "demo", "stelvio:env": "other"},
        {"stelvio:app": "other", "stelvio:env": "test"},
        {"stelvio:app": "demo"},
        {},
    ],
)
def test_database_audit_checks_compact_names_on_all_pages(
    operation, field, identifier, arn_key, tags
):
    session = _empty_session()
    client = session.clients["docdb"]
    arn = "arn:aws:rds:us-east-1:123456789012:cluster:stlv-compact-orders"
    client.pages[operation] = [
        {field: []},
        {field: [{identifier: "stlv-compact-orders", arn_key: arn}]},
    ]
    client.direct["list_tags_for_resource"] = {
        "TagList": [{"Key": key, "Value": value} for key, value in tags.items()]
    }
    remaining = assert_tunnel._database_resources("demo", "test", sdk=session)
    expected = tags == {"stelvio:app": "demo", "stelvio:env": "test"}
    assert remaining == ([f"DocumentDB {identifier} stlv-compact-orders"] if expected else [])
    assert ("list_tags_for_resource", {"ResourceName": arn}) in client.calls


@pytest.mark.parametrize("missing_arn", [False, True])
def test_database_audit_refuses_unverified_ownership(missing_arn):
    session = _empty_session()
    client = session.clients["docdb"]
    item = {"DBClusterIdentifier": "stlv-compact-orders"}
    if not missing_arn:
        item["DBClusterArn"] = "arn:aws:rds:us-east-1:123456789012:cluster:stlv-compact-orders"
    client.pages["describe_db_clusters"] = [{"DBClusters": [item]}]

    def fail(**_kwargs):
        raise ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListTagsForResource"
        )

    client.direct["list_tags_for_resource"] = fail
    with pytest.raises(AssertionError if missing_arn else ClientError):
        assert_tunnel._database_resources("demo", "test", sdk=session)


@pytest.fixture
def command_session(tmp_path, monkeypatch):
    session = cli_session.StandaloneCliSession.__new__(cli_session.StandaloneCliSession)
    session.project = tmp_path
    session.app = "proof-app"
    session.environment_name = "test"
    session.cli = tmp_path / "stlv"
    session.environment = {}
    session.aws = object()
    session.no_vpc = False
    session._deployed = True
    session.process = None
    session._outputs = {}
    monkeypatch.setattr(cli_session, "INSTALLED_HELPER", tmp_path / "helper")
    return session


@pytest.mark.parametrize("outcome", ["success", "failure", "timeout"])
def test_cli_command_retains_private_output_and_status(monkeypatch, command_session, outcome):
    import json
    import stat
    import subprocess

    def run(*args, **kwargs):
        assert kwargs["timeout"] == cli_session.DESTROY_TIMEOUT
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(
                args[0], kwargs["timeout"], b"partial output", b"partial error"
            )
        return SimpleNamespace(
            returncode=7 if outcome == "failure" else 0, stdout="output", stderr="error"
        )

    monkeypatch.setattr(cli_session.subprocess, "run", run)
    if outcome == "success":
        assert command_session.command("destroy", "test", "--yes") == "output"
    else:
        expected = subprocess.TimeoutExpired if outcome == "timeout" else RuntimeError
        with pytest.raises(expected) as captured:
            command_session.command("destroy", "test", "--yes")
        detail = str(captured.value) + " ".join(getattr(captured.value, "__notes__", []))
        assert "proof-app/test" in detail
        assert str(command_session.project) in detail
        assert "unlock test" in detail
        assert "first verify no deployment operation" in detail
    directories = list(command_session.project.glob("cli-destroy-*"))
    assert len(directories) == 1
    directory = directories[0]
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for item in directory.iterdir():
        assert stat.S_IMODE(item.stat().st_mode) == 0o600
    metadata = json.loads((directory / "command.json").read_text())
    assert metadata["timed_out"] == (outcome == "timeout")
    assert metadata["exit_status"] == {"success": 0, "failure": 7, "timeout": None}[outcome]
    assert metadata["arguments"] == ["destroy", "test", "--yes"]
    assert (directory / "stdout.log").read_text() == (
        "partial output" if outcome == "timeout" else "output"
    )
    assert (directory / "stderr.log").read_text() == (
        "partial error" if outcome == "timeout" else "error"
    )


def test_deploy_keeps_startup_deadline(monkeypatch, command_session):
    calls = []

    def command(*args, **options):
        calls.append((args, options))
        raise RuntimeError("stop after deployment command")

    command_session.command = command
    with pytest.raises(RuntimeError, match="stop after deployment"):
        command_session.deploy()
    assert calls == [(("deploy", "test", "--yes"), {"timeout": 900})]


@pytest.mark.parametrize("timeout", [False, True])
def test_destroy_uses_separate_budget_and_audits_failures(monkeypatch, command_session, timeout):
    import subprocess

    cli_session.INSTALLED_HELPER.touch()
    calls = []
    monkeypatch.setattr(
        cli_session,
        "NativeHelper",
        lambda: SimpleNamespace(
            status=lambda: SimpleNamespace(owned=False, uncertain=False, mutating=False, units=[])
        ),
    )
    monkeypatch.setattr(cli_session, "capture_proof_volume_ids", lambda **kwargs: ())

    def command(*args, **options):
        calls.append((args, options))
        if timeout:
            raise subprocess.TimeoutExpired(args, options["timeout"])

    command_session.command = command
    command_session._assert_aws_resources_absent = lambda: calls.append("audit")
    command_session._clear_proof_host_trust = lambda: calls.append("trust-clear")
    if timeout:
        with pytest.raises(subprocess.TimeoutExpired):
            command_session.destroy()
    else:
        command_session.destroy()
    assert calls[:2] == [(("destroy", "test", "--yes"), {"timeout": 3600}), "audit"]
    assert ("trust-clear" in calls) is not timeout
    assert all(entry[0][0] == "destroy" for entry in calls if isinstance(entry, tuple))


@pytest.mark.parametrize("no_vpc", [False, True])
@pytest.mark.parametrize(
    "helper_state", ["absent", "idle", "owned", "uncertain", "mutating", "units"]
)
def test_destroy_requires_certified_host_state(monkeypatch, command_session, no_vpc, helper_state):
    command_session.no_vpc = no_vpc
    if helper_state != "absent":
        cli_session.INSTALLED_HELPER.touch()
    state = {"owned": False, "uncertain": False, "mutating": False, "units": []}
    if helper_state in {"owned", "uncertain", "mutating"}:
        state[helper_state] = True
    elif helper_state == "units":
        state["units"] = ["proof-unit"]
    helper = Mock(return_value=SimpleNamespace(status=lambda: SimpleNamespace(**state)))
    monkeypatch.setattr(cli_session, "NativeHelper", helper)
    operations = []
    monkeypatch.setattr(
        cli_session,
        "capture_proof_volume_ids",
        lambda **kwargs: operations.append("capture") or (),
    )
    command_session.command = lambda *args, **kwargs: operations.append("destroy")
    command_session._assert_aws_resources_absent = lambda: operations.append("audit")
    command_session._clear_proof_host_trust = lambda: operations.append("trust-clear")

    allowed = helper_state == "idle" or (helper_state == "absent" and no_vpc)
    if allowed:
        command_session.destroy()
        assert operations == ["capture", "destroy", "audit", "trust-clear"]
    else:
        reason = (
            "helper is unavailable" if helper_state == "absent" else "cleanup is not certified"
        )
        with pytest.raises(RuntimeError, match=reason):
            command_session.destroy()
        assert operations == []
    if helper_state == "absent":
        helper.assert_not_called()
    else:
        helper.assert_called_once_with()
