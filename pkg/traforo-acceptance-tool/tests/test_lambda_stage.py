import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from test_safety import PROOF, SDK

from traforo_acceptance import aws, candidate, lambda_aws, recovery, runner
from traforo_acceptance.report import generate
from traforo_acceptance.stages import DOCUMENTDB_STAGES, LAMBDA_TESTS, STAGES, select
from traforo_acceptance.store import Store, event, events, write_json


def error(code, operation="GetRestApi"):
    return ClientError({"Error": {"Code": code}}, operation)


@pytest.fixture
def proof(run_root):
    project = run_root / "app"
    project.mkdir()
    owned = {**PROOF, "project": str(project), "suite": "lambda-vpc"}
    write_json(project / ".acceptance-owner.json", owned)
    event(run_root, "proof_intent", **owned)
    return owned


@pytest.fixture
def api_sdk(proof):
    values = {}
    sdk = SDK({("sts", "get_caller_identity"): {"Account": proof["account"]}})
    sdk.responses[("apigateway", "get_rest_apis")] = lambda p: {"items": list(values.values())}

    def get(parameters):
        if parameters["restApiId"] not in values:
            raise error("NotFoundException")
        return values[parameters["restApiId"]]

    sdk.responses[("apigateway", "get_rest_api")] = get
    sdk.responses[("apigateway", "delete_rest_api")] = lambda p: values.pop(p["restApiId"], None)
    return sdk, values


def register(root, sdk, values, proof, number, *, confirmed=True):
    name = lambda_aws.expected_name(proof, number)
    lambda_aws.before_create(root, sdk, proof, number, name)
    identifier = f"api{number}"
    values[identifier] = {
        "id": identifier,
        "name": name,
        "tags": {"stelvio:app": proof["app"], "stelvio:env": "proof"},
        "endpointConfiguration": {"types": ["PRIVATE"]},
    }
    if confirmed:
        event(root, "api_created", **proof, number=number, name=name, api_id=identifier)
    return identifier


def test_default_stage_order_and_networking_only_selection():
    selected = select((), ())
    assert selected.index("aws-lambda-vpc") < selected.index("aws-standalone")
    assert "aws-lambda-vpc" in select(("aws",), ())
    assert "aws-lambda-vpc" not in select((), ("aws",))
    assert select(("aws-lambda-vpc",), ()) == ["prepare", "build", "distribution", "aws-lambda-vpc"]
    assert STAGES["aws-lambda-vpc"].timeout == 7200
    assert "aws-lambda-vpc" not in DOCUMENTDB_STAGES
    assert "aws-non-vpc" not in DOCUMENTDB_STAGES


def test_command_runs_shared_module_once_with_both_flags(run_root):
    commands = runner.stage_commands("aws-lambda-vpc", run_root, {})
    assert len(commands) == 1
    argv, cwd = commands[0]
    assert argv.count("tests/integration/test_tunnel_lambda.py") == 1
    assert "--integration-macos-privileged" in argv and "--integration-tunnel" in argv
    assert argv[argv.index("-n") + 1] == "0"
    assert cwd == run_root / "source"


def test_seven_call_results_required_and_skip_is_not_pass(run_root):
    names = sorted(LAMBDA_TESTS)
    for name in names[:-1]:
        event(
            run_root,
            "test_result",
            stage="aws-lambda-vpc",
            phase="call",
            outcome="passed",
            nodeid=f"tests/integration/test_tunnel_lambda.py::{name}",
        )
    with pytest.raises(RuntimeError, match="seven"):
        runner.validate_lambda_results(run_root)
    event(
        run_root,
        "test_result",
        stage="aws-lambda-vpc",
        phase="call",
        outcome="passed",
        nodeid=f"tests/integration/test_tunnel_lambda.py::{names[-1]}",
    )
    runner.validate_lambda_results(run_root)


def test_exclusion_is_explicit_and_round_partial(run_root):
    store = Store(run_root)
    data = store.data
    data["selected"] = select((), ("aws-lambda-vpc",))
    data["results"] = {n: {"outcome": "passed"} for n in data["selected"]}
    data["audit"] = {"outcome": "certified"}
    store.save(data)
    report = generate(run_root)
    assert report["outcome"] == "partial-passed"
    assert "stage: aws-lambda-vpc" in report["exclusions"]


def test_adapter_missing_fixture_fails_before_tools(tmp_path):
    # The original stack passes ordinary interface checks but lacks PR #411.
    fixtures = {
        "tests/integration/cli_session.py": (
            "class StandaloneCliSession: pass\ndef deploy(): pass\ndef destroy(): pass"
        ),
        "tests/integration/macos_host.py": (
            "class MacOSHost: pass\ndef prepare(): pass\ndef save(): pass\ndef restore(): pass"
        ),
        "pkg/traforo-client-go/scripts/build.py": (
            "def build(): pass\ndef goreleaser_version(): pass"
        ),
    }
    for relative, text in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    with pytest.raises(RuntimeError, match="ea1d9144, e196a31d, and 49eafd2a"):
        candidate.validate(tmp_path, ["aws-lambda-vpc"])


def test_api1_missing_does_not_hide_api2(run_root, proof, api_sdk):
    sdk, values = api_sdk
    first = register(run_root, sdk, values, proof, 1)
    register(run_root, sdk, values, proof, 2)
    del values[first]
    view = lambda_aws.api_inventory(run_root, aws.Reader(sdk), proof)
    assert view["1"]["outcome"] == "absent"
    assert view["2"]["outcome"] == "remaining"
    assert recovery.proofs(run_root)[proof["app"]] == proof


def test_lost_creation_reply_is_discovered_without_repeating_create(run_root, proof, api_sdk):
    sdk, values = api_sdk
    register(run_root, sdk, values, proof, 1, confirmed=False)
    assert lambda_aws.api_inventory(run_root, aws.Reader(sdk), proof)["1"]["api_id"] == "api1"
    assert any(r["kind"] == "api_discovered" for r in events(run_root))
    result = lambda_aws.recover_apis(run_root, sdk, proof)
    assert all(a["outcome"] == "completed" for a in result)
    assert not values
    assert not any(operation == "create_rest_api" for _, operation, _ in sdk.calls)


def test_unconfirmed_creation_cannot_be_certified_by_empty_listing(run_root, proof, api_sdk):
    sdk, _ = api_sdk
    lambda_aws.before_create(run_root, sdk, proof, 1, f"{proof['app']}-api1")
    with pytest.raises(RuntimeError, match="empty listing"):
        lambda_aws.api_inventory(run_root, aws.Reader(sdk), proof)
    with pytest.raises(RuntimeError, match="already attempted"):
        lambda_aws.before_create(run_root, sdk, proof, 1, f"{proof['app']}-api1")


def test_existing_namespace_is_not_claimed(run_root, proof, api_sdk):
    sdk, values = api_sdk
    values["foreign"] = {"name": f"{proof['app']}-api1"}
    with pytest.raises(RuntimeError, match="not empty"):
        lambda_aws.before_create(run_root, sdk, proof, 1, f"{proof['app']}-api1")
    assert not any(r["kind"] == "api_intent" for r in events(run_root))


def test_foreign_api_failure_does_not_prevent_peer_cleanup(run_root, proof, api_sdk):
    sdk, values = api_sdk
    register(run_root, sdk, values, proof, 1)
    register(run_root, sdk, values, proof, 2)
    values["api1"]["tags"] = {"stelvio:app": "foreign"}
    actions = lambda_aws.recover_apis(run_root, sdk, proof)
    assert [a["outcome"] for a in actions] == ["failed", "completed"]
    assert "api1" in values and "api2" not in values


def test_lost_delete_reply_is_read_not_repeated(run_root, proof, api_sdk):
    sdk, values = api_sdk
    register(run_root, sdk, values, proof, 1)

    def lost(parameters):
        del values[parameters["restApiId"]]
        raise TimeoutError("reply lost")

    sdk.responses[("apigateway", "delete_rest_api")] = lost
    assert lambda_aws.recover_apis(run_root, sdk, proof)[0]["outcome"] == "completed"
    assert sum(op == "delete_rest_api" for _, op, _ in sdk.calls) == 1


def test_unknown_delete_does_not_retry_surviving_api(run_root, proof, api_sdk):
    sdk, values = api_sdk
    register(run_root, sdk, values, proof, 1)
    sdk.responses[("apigateway", "delete_rest_api")] = TimeoutError("reply lost")
    assert lambda_aws.recover_apis(run_root, sdk, proof)[0]["outcome"] == "failed"
    assert sum(op == "delete_rest_api" for _, op, _ in sdk.calls) == 1
    assert values


def test_throttling_only_bounded_and_no_uncertain_retry(api_sdk, monkeypatch):
    sdk, _ = api_sdk
    sdk.responses[("apigateway", "delete_rest_api")] = error("TooManyRequestsException")
    sleeps = []
    monkeypatch.setattr(lambda_aws.time, "sleep", sleeps.append)
    with pytest.raises(ClientError):
        lambda_aws.retry_throttling(aws.Reader(sdk), "delete_rest_api", restApiId="api1")
    assert len(sleeps) == 9
    assert sum(sleeps) <= 90
    sdk.calls.clear()
    sdk.responses[("apigateway", "delete_rest_api")] = TimeoutError("unknown")
    with pytest.raises(TimeoutError):
        lambda_aws.retry_throttling(aws.Reader(sdk), "delete_rest_api", restApiId="api1")
    assert len(sdk.calls) == 1


def test_api_pagination_uses_position():
    sdk = SDK(
        {
            ("apigateway", "get_rest_apis"): lambda p: (
                {"items": [{"id": "second"}]}
                if p.get("position")
                else {"items": [{"id": "first"}], "position": "next"}
            )
        }
    )
    assert [v["id"] for v in aws.Reader(sdk).pages("apigateway", "get_rest_apis", "items")] == [
        "first",
        "second",
    ]


def test_lambda_inventory_needs_no_documentdb_permissions(run_root, proof, api_sdk):
    sdk, _ = api_sdk
    sdk.responses[("docdb", "describe_db_clusters")] = PermissionError("not allowed")
    snapshot = lambda_aws.capture_proof(run_root, sdk, proof)
    assert not snapshot["remaining"]
    assert not any(service in {"docdb", "secretsmanager"} for service, _, _ in sdk.calls)
    assert set(snapshot["vpc_views"]) == {"1", "2"}


def test_deleted_endpoint_does_not_hide_peer_or_interface(proof):
    sdk = SDK({("sts", "get_caller_identity"): {"Account": proof["account"]}})

    def endpoints(p):
        if p.get("VpcEndpointIds") == ["vpce-first"]:
            raise error("InvalidVpcEndpointId.NotFound")
        if p.get("VpcEndpointIds") == ["vpce-second"]:
            return {
                "VpcEndpoints": [
                    {
                        "VpcEndpointId": "vpce-second",
                        "VpcId": "vpc2",
                        "State": "available",
                        "NetworkInterfaceIds": ["eni2"],
                    }
                ]
            }
        return {}

    sdk.responses[("ec2", "describe_vpc_endpoints")] = endpoints
    sdk.responses[("ec2", "describe_network_interfaces")] = lambda p: (
        {"NetworkInterfaces": [{"NetworkInterfaceId": "eni2", "VpcId": "vpc2"}]}
        if p.get("NetworkInterfaceIds") == ["eni2"]
        else {}
    )
    prior = [
        {
            "endpoint_records": [
                {"id": "vpce-first", "vpc": "vpc1", "interfaces": []},
                {"id": "vpce-second", "vpc": "vpc2", "interfaces": ["eni2"]},
            ]
        }
    ]
    result = aws.capture(sdk, proof, prior)
    assert result["remaining"]["captured_endpoints"] == ["vpce-second"]
    assert result["remaining"]["captured_interfaces"] == ["eni2"]
    assert result["endpoint_records"][0]["outcome"] == "absent"


def test_api_failure_does_not_prevent_application_destroy(run_root, proof, api_sdk, monkeypatch):
    sdk, values = api_sdk
    register(run_root, sdk, values, proof, 1)
    register(run_root, sdk, values, proof, 2)
    values["api1"]["tags"] = {"stelvio:app": "foreign"}
    event(run_root, "application_deploy_intent", **proof)
    monkeypatch.setattr(recovery, "sdk_for", lambda p: sdk)
    commands = []
    actions = recovery.recover(run_root, lambda *args: commands.append(args), {})
    assert any(a.get("api_number") == 1 and a["outcome"] == "failed" for a in actions)
    assert any(a.get("api_number") == 2 and a["outcome"] == "completed" for a in actions)
    assert any(a.get("operation") == "destroy" and a["outcome"] == "completed" for a in actions)
    assert len(commands) == 1 and commands[0][0] == "recover-destroy"
    assert "api1" in values and "api2" not in values


def test_api_only_failure_does_not_destroy_nonexistent_app(run_root, proof, api_sdk, monkeypatch):
    sdk, values = api_sdk
    register(run_root, sdk, values, proof, 1)
    lambda_aws.before_create(run_root, sdk, proof, 2, f"{proof['app']}-api2")
    event(run_root, "api_not_created", **proof, number=2, reason="explicit-throttle")
    monkeypatch.setattr(recovery, "sdk_for", lambda p: sdk)
    actions = recovery.recover(run_root, lambda *a: pytest.fail("App deployment never started"), {})
    assert not values
    assert all(a["outcome"] == "completed" for a in actions)
    assert any(a.get("application") == "not-started" for a in actions)


def test_independent_audit_retains_other_vpc_when_one_api_read_fails(run_root, proof, api_sdk):
    sdk, values = api_sdk
    register(run_root, sdk, values, proof, 1)
    register(run_root, sdk, values, proof, 2)
    values["api1"]["tags"]["stelvio:env"] = "foreign"
    result = lambda_aws.capture_proof(run_root, sdk, proof)
    assert result["vpc_views"]["1"]["api"]["outcome"] == "unknown"
    assert result["vpc_views"]["2"]["api"]["outcome"] == "remaining"
    assert result["unknown"]


def test_incomplete_faults_resolve_only_after_exact_bastion_absence(run_root, proof, api_sdk):
    sdk, _ = api_sdk
    event(
        run_root,
        "fault_enter",
        **proof,
        fault_id="fault1",
        fault="bastion-stop",
        instance_id="i-proof",
    )
    instance = {
        "InstanceId": "i-proof",
        "State": {"Name": "stopped"},
        "Tags": [
            {"Key": "stelvio:app", "Value": proof["app"]},
            {"Key": "stelvio:env", "Value": proof["env"]},
        ],
    }
    sdk.responses[("ec2", "describe_instances")] = {
        "Reservations": [{"OwnerId": proof["account"], "Instances": [instance]}]
    }
    snapshot = lambda_aws.capture_proof(run_root, sdk, proof)
    assert snapshot["faults"][0]["outcome"] == "uncertain"
    assert snapshot["remaining"]["faults"]
    instance["State"]["Name"] = "terminated"
    snapshot = lambda_aws.capture_proof(run_root, sdk, proof)
    assert snapshot["faults"][0]["outcome"] == "interrupted-bastion-removed"
    assert "faults" not in snapshot["remaining"]
    assert not any(
        op in {"start_instances", "stop_instances", "send_command"} for _, op, _ in sdk.calls
    )


@pytest.fixture
def observed(run_root, proof, api_sdk, monkeypatch):
    """A fake candidate, including subclass overrides and base-method delegation."""
    sdk, values = api_sdk
    sdk.responses[("lambda", "get_function")] = error("ResourceNotFoundException")
    from traforo_acceptance import pytest_plugin

    class Api:
        def __init__(self, number):
            self.number = number
            self.name = f"{proof['app']}-api{number}"
            self.api_id = None
            self.evidence = run_root / f"private-api-{number}.json"

        def create(self):
            assert any(r["kind"] == "proof_intent" for r in events(run_root))
            assert any(
                r["kind"] == "api_intent" and r["number"] == self.number for r in events(run_root)
            )
            if getattr(self, "fail", False):
                raise TimeoutError("lost create response")
            self.api_id = f"api{self.number}"
            values[self.api_id] = {
                "id": self.api_id,
                "name": self.name,
                "tags": {"stelvio:app": proof["app"], "stelvio:env": "proof"},
                "endpointConfiguration": {"types": ["PRIVATE"]},
            }
            self.evidence.write_text(json.dumps({"api_id": self.api_id}))

        def destroy(self):
            values.pop(self.api_id, None)

    class Base:
        def deploy(self):
            pytest.fail("Subclass must implement deploy")

        def command(self, *args, **kwargs):
            return "{}"

        def destroy(self):
            pass

        def _start(self, *args, **kwargs):
            self.process = SimpleNamespace(pid=123, args=args[0], poll=lambda: None)

        def wait_host_key_refusal(self, instance_id):
            if getattr(self, "refusal_fails", False):
                raise AssertionError("wrong startup status")
            return "cause=ssh-host-key"

        @contextmanager
        def rotated_host_key(self, instance):
            self.key_changed = True
            try:
                yield "ssh-ed25519 cHVibGlj"
            finally:
                self.key_changed = False

    class Session(Base):
        def __init__(self):
            self.app, self.environment_name = proof["app"], proof["env"]
            self.profile, self.account, self.region = (
                proof["profile"],
                proof["account"],
                proof["region"],
            )
            self.project = Path(proof["project"])
            self.aws, self.apis = sdk, {1: Api(1), 2: Api(2)}
            self._outputs, self._manifest = {}, None
            self.log_path = self.project / "cli.log"

        def deploy(self):
            for api in self.apis.values():
                api.create()
            self.command("deploy", self.environment_name, "--yes")
            self._manifest = SimpleNamespace(
                used_vpcs=[
                    SimpleNamespace(identity=f"aws::net{n}", vpc_id=f"vpc{n}") for n in (1, 2, 3)
                ]
            )
            prefix = f"{self.app}-{self.environment_name}"
            for number in self.apis:
                self._outputs.update(
                    {
                        f"endpoint{number}_id": f"vpce{number}",
                        f"function{number}_name": f"{prefix}-client{number}",
                        f"backend{number}_name": f"{prefix}-echo{number}",
                    }
                )
            for number in (3, 4):
                self._outputs[f"function{number}_name"] = f"{prefix}-client{number}"
            self.write_evidence()

        def destroy(self):
            for api in self.apis.values():
                api.destroy()
            super().destroy()
            self.write_evidence()

        def write_evidence(self):
            pass

        def network(self, number):
            return SimpleNamespace(
                vpc_id=f"vpc{number}", access=SimpleNamespace(instance_id=f"i{number}")
            )

        @contextmanager
        def stopped_bastion(self, number):
            self.stopped = True
            try:
                yield
            finally:
                self.stopped = False

    # Exercise the actual external plugin, not a second implementation of its hooks.
    import sys
    from types import ModuleType

    modules = {}
    for name in (
        "tests.integration.cli_session",
        "tests.integration.macos_host",
        "tests.integration.lambda_tunnel_session",
        "stelvio.tunnel",
    ):
        modules[name] = ModuleType(name)
        monkeypatch.setitem(sys.modules, name, modules[name])
    modules["tests.integration.cli_session"].StandaloneCliSession = Base
    modules["tests.integration.macos_host"].MacOSHost = type("Host", (), {"save": lambda *a: None})
    modules["tests.integration.lambda_tunnel_session"].LambdaTunnelSession = Session
    modules["tests.integration.lambda_tunnel_session"].PrivateApi = Api
    modules["stelvio.tunnel"].installation = SimpleNamespace(
        _bootstrap=lambda asset, digest, *, cleanup=False: cleanup
    )
    monkeypatch.setattr(pytest_plugin, "ROOT", run_root)
    monkeypatch.setenv("TRA_ACCEPTANCE_STAGE", "aws-lambda-vpc")
    pytest_plugin.pytest_configure(SimpleNamespace(getoption=lambda *args, **kwargs: True))
    return Session(), sdk, values


def test_subclass_registration_and_shared_deployment_are_not_duplicated(run_root, observed):
    session, _, _ = observed
    session.deploy()
    session.destroy()
    records = events(run_root)
    # The fixture's proof already registered once; this invocation adds exactly one.
    assert sum(r["kind"] == "proof_intent" for r in records) == 2
    assert sum(r["kind"] == "application_deploy_intent" for r in records) == 1
    assert sum(r["kind"] == "proof_destroyed" for r in records) == 1
    assert sum(r["kind"] == "api_created" for r in records) == 2
    topology = next(r["targets"] for r in records if r["kind"] == "lambda_topology")
    assert len(topology) == 2
    assert all(len(v["functions"]) == 2 for v in topology.values())
    record = next(r for r in records if r["kind"] == "lambda_topology")
    assert len(record["functions"]) == 6
    assert len(record["vpcs"]) == 3
    assert record["clients"]["3"]["networking"] == "disabled"
    assert record["clients"]["3"]["vpc_id"] == "vpc3"
    assert record["clients"]["4"] == {
        "name": f"{session.app}-{session.environment_name}-client4",
        "vpc_id": None,
        "networking": "none",
    }
    # Only the two managed targets require access metadata.
    assert set(topology) == {"1", "2"}


def test_second_creation_failure_retains_first_receipt_and_no_deploy_intent(run_root, observed):
    session, _, _ = observed
    session.apis[2].fail = True
    with pytest.raises(TimeoutError, match="lost create"):
        session.deploy()
    assert session.apis[1].evidence.is_file()
    assert not any(r["kind"] == "application_deploy_intent" for r in events(run_root))
    assert any(r["kind"] == "api_unconfirmed" and r["number"] == 2 for r in events(run_root))


def test_fault_observer_retains_failure_and_never_records_key_material(run_root, observed):
    session, _, _ = observed
    with pytest.raises(AssertionError), session.stopped_bastion(1):
        raise AssertionError("fault assertion")
    assert session.stopped is False
    assert any(r["kind"] == "fault_unfinished" for r in events(run_root))
    with session.rotated_host_key("i1"):
        assert session.key_changed
    assert not session.key_changed
    assert "cHVibGlj" not in (run_root / "events.jsonl").read_text()
    assert any(
        r["kind"] == "fault_established" and r["public_fingerprint"].startswith("SHA256:")
        for r in events(run_root)
    )


def test_private_api_account_mismatch_never_claims_or_deletes(run_root, proof, api_sdk):
    sdk, values = api_sdk
    sdk.responses[("sts", "get_caller_identity")] = {"Account": "999999999999"}
    with pytest.raises(RuntimeError, match="identity changed"):
        lambda_aws.before_create(run_root, sdk, proof, 1, f"{proof['app']}-api1")
    assert not values
    assert not any(r["kind"] == "api_intent" for r in events(run_root))
    assert not any(service == "apigateway" for service, _, _ in sdk.calls)


def test_lambda_skip_or_failed_call_prevents_acceptance(run_root):
    for index, name in enumerate(sorted(LAMBDA_TESTS)):
        event(
            run_root,
            "test_result",
            stage="aws-lambda-vpc",
            phase="call",
            nodeid=f"module::{name}",
            outcome="skipped" if index == 0 else "passed",
        )
    with pytest.raises(RuntimeError, match="all seven"):
        runner.validate_lambda_results(run_root)


def test_gateway_and_captured_endpoint_requests_match_sdk_models(run_root, proof, api_sdk):
    import botocore.session
    from botocore import xform_name
    from botocore.validate import validate_parameters

    sdk, values = api_sdk
    register(run_root, sdk, values, proof, 1)
    original = sdk.client
    models = botocore.session.get_session()

    def client(service, **kwargs):
        actual = original(service, **kwargs)
        model = models.get_service_model(service)

        class Validated:
            def __getattr__(self, name):
                shape = model.operation_model(
                    next(op for op in model.operation_names if xform_name(op) == name)
                ).input_shape

                def call(**arguments):
                    validate_parameters(arguments, shape)
                    return getattr(actual, name)(**arguments)

                return call

        return Validated()

    sdk.client = client
    event(
        run_root,
        "lambda_topology",
        **proof,
        targets={"1": {"endpoint_id": "vpce-123", "vpc_id": "vpc-123", "instance_id": "i-123"}},
    )
    lambda_aws.capture_proof(run_root, sdk, proof)
    assert lambda_aws.recover_apis(run_root, sdk, proof)[0]["outcome"] == "completed"


@pytest.mark.parametrize("change", ["duplicate", "unexpected", "skipped", "failed"])
def test_seven_case_gate_rejects_invalid_call_sets(run_root, change):
    names = sorted(LAMBDA_TESTS)
    assert len(names) == 7
    for index, name in enumerate(names):
        event(
            run_root,
            "test_result",
            stage="aws-lambda-vpc",
            phase="call",
            nodeid=f"module::{name if change != 'unexpected' or index else 'test_unexpected'}",
            outcome=change if index == 0 and change in {"skipped", "failed"} else "passed",
        )
    if change == "duplicate":
        event(
            run_root,
            "test_result",
            stage="aws-lambda-vpc",
            phase="call",
            nodeid=f"module::{names[0]}",
            outcome="passed",
        )
    with pytest.raises(RuntimeError, match="all seven"):
        runner.validate_lambda_results(run_root)


def test_topology_survives_partial_deployment_without_manifest(run_root, observed):
    session, _, _ = observed
    session._outputs = {"function3_name": f"{session.app}-{session.environment_name}-client3"}
    session.write_evidence()
    record = next(r for r in reversed(events(run_root)) if r["kind"] == "lambda_topology")
    assert record["functions"] == list(session._outputs.values())
    assert record["targets"] == {} and record["vpcs"] == []
    assert record["clients"]["3"]["networking"] == "disabled"
    assert record["clients"]["3"]["vpc_id"] is None


@pytest.mark.parametrize("survivor", ["client3", "client4", "vpc3"])
def test_exact_inventory_finds_plain_peer_after_other_resources_are_absent(
    run_root, proof, api_sdk, survivor
):
    sdk, _ = api_sdk
    prefix = f"{proof['app']}-{proof['env']}-"
    names = [
        f"{prefix}{kind}{n}"
        for kind, numbers in (("client", (1, 2, 3, 4)), ("backend", (1, 2)))
        for n in numbers
    ]
    vpcs = [{"id": f"vpc{n}", "identity": f"aws::net{n}"} for n in (1, 2, 3)]
    event(
        run_root,
        "lambda_topology",
        **proof,
        targets={},
        functions=names,
        vpcs=vpcs,
        clients={"3": {"networking": "disabled"}},
    )

    def function(args):
        name = args["FunctionName"]
        if name != prefix + survivor:
            raise error("ResourceNotFoundException")
        return {
            "Configuration": {
                "FunctionName": name,
                "FunctionArn": (
                    f"arn:aws:lambda:{proof['region']}:{proof['account']}:function:{name}"
                ),
            }
        }

    def vpc(args):
        if args.get("VpcIds") != [survivor]:
            if "VpcIds" in args:
                raise error("InvalidVpcID.NotFound")
            return {}
        return {
            "Vpcs": [
                {
                    "VpcId": survivor,
                    "OwnerId": proof["account"],
                    "Tags": [
                        {"Key": "stelvio:app", "Value": proof["app"]},
                        {"Key": "stelvio:env", "Value": proof["env"]},
                    ],
                }
            ]
        }

    sdk.responses[("lambda", "get_function")] = function
    sdk.responses[("ec2", "describe_vpcs")] = vpc
    snapshot = lambda_aws.capture_proof(run_root, sdk, proof)
    key = "captured_vpcs" if survivor == "vpc3" else "captured_functions"
    assert snapshot["remaining"] == {key: [survivor if survivor == "vpc3" else prefix + survivor]}
    assert len([1 for s, op, _ in sdk.calls if s == "lambda" and op == "get_function"]) == 6
    assert len([1 for s, op, a in sdk.calls if op == "describe_vpcs" and "VpcIds" in a]) == 3
    assert snapshot["vpc_views"]["3"]["networking"] == "disabled"
    assert not any(s == "docdb" for s, _, _ in sdk.calls)


@pytest.mark.parametrize("failed", [False, True])
def test_each_command_and_host_key_startup_refusal_is_recorded(
    run_root, observed, monkeypatch, failed
):
    from traforo_acceptance import ownership

    session, _, _ = observed
    monkeypatch.setattr(ownership, "process_record", lambda pid: {"pid": pid, "uid": 502})
    session.refusal_fails = failed
    for _ in range(2):
        session._start(["tunnel", "start"], mode="tunnel")
        session.process.poll = lambda: 1
        if failed:
            with pytest.raises(AssertionError):
                session.wait_host_key_refusal("i1")
        else:
            session.wait_host_key_refusal("i1")
    records = events(run_root)
    assert sum(r["kind"] == "proof_command" for r in records) == 2
    refusals = [r for r in records if r["kind"] == "startup_refusal_result"]
    assert len(refusals) == 2
    assert all(r["outcome"] == ("failed" if failed else "passed") for r in refusals)
    assert all(r["exit_code"] == 1 and r["instance_id"] == "i1" for r in refusals)


@pytest.mark.parametrize(
    "missing", [None, "check_plain_clients", "wait_host_key_refusal", "new_cases"]
)
def test_candidate_requires_expanded_lambda_interfaces(tmp_path, missing):
    cases = sorted(LAMBDA_TESTS)
    if missing == "new_cases":
        cases.remove("test_lambda_dev_sigkill_and_restart")
        cases.remove("test_lambda_standalone_host_key_startup_refusal")
    methods = {"deploy", "destroy", "stopped_bastion", "write_evidence", "check_plain_clients"}
    base = {"deploy", "destroy", "wait_host_key_refusal"}
    methods.discard(missing)
    base.discard(missing)
    fixtures = {
        "tests/integration/cli_session.py": "class StandaloneCliSession:\n"
        + "".join(f"    def {name}(self): pass\n" for name in sorted(base)),
        "tests/integration/macos_host.py": (
            "class MacOSHost: pass\ndef prepare(): pass\ndef save(): pass\ndef restore(): pass"
        ),
        "pkg/traforo-client-go/scripts/build.py": (
            "def build(): pass\ndef goreleaser_version(): pass"
        ),
        "pkg/traforo-client-go/.github/workflows/release.yml": "version: v2.18.3",
        "tests/integration/lambda_tunnel_session.py": (
            "class LambdaTunnelSession:\n"
            + "".join(f"    def {name}(self): pass\n" for name in sorted(methods))
            + "class PrivateApi:\n    def create(self): pass\n    def destroy(self): pass\n"
            "def retry_api_throttling(): pass\n"
        ),
        "tests/integration/test_tunnel_lambda.py": "\n".join(f"def {n}(): pass" for n in cases),
        **dict.fromkeys(
            [
                "tests/integration/test_tunnel.py",
                "tests/integration/test_tunnel_host.py",
                "tests/integration/test_tunnel_dns.py",
                "tests/integration/handlers/tunnel_lambda_app.py.txt",
                "tests/integration/handlers/tunnel_lambda_probe.py",
            ],
            "",
        ),
    }
    for relative, text in fixtures.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if missing:
        with pytest.raises(RuntimeError, match="unsupported interface|Unsupported Lambda"):
            candidate.validate(tmp_path, ["aws-lambda-vpc"])
    else:
        candidate.validate(tmp_path, ["aws-lambda-vpc"])


@pytest.mark.parametrize("resource", ["vpc", "function"])
def test_exact_plain_resource_identity_mismatch_is_not_absence(run_root, proof, api_sdk, resource):
    sdk, _ = api_sdk
    name = f"{proof['app']}-{proof['env']}-client4"
    if resource == "vpc":
        sdk.responses[("ec2", "describe_vpcs")] = lambda args: (
            {"Vpcs": [{"VpcId": "vpc3", "OwnerId": "999999999999"}]} if "VpcIds" in args else {}
        )
        previous = [{"vpc_records": [{"id": "vpc3"}]}]
    else:
        sdk.responses[("lambda", "get_function")] = {
            "Configuration": {
                "FunctionName": name,
                "FunctionArn": "arn:aws:lambda:us-east-1:999999999999:function:foreign",
            }
        }
        previous = [{"functions": [name]}]
    with pytest.raises(RuntimeError, match="ownership changed|identity changed"):
        aws.capture(sdk, proof, previous)


def test_log_cleanup_refuses_exact_remaining_plain_function(run_root, proof, api_sdk, monkeypatch):
    sdk, _ = api_sdk
    event(run_root, "application_deploy_intent", **proof)
    snapshot = {"remaining": {"captured_functions": ["client4"], "log_groups": ["log4"]}}
    monkeypatch.setattr(recovery, "capture_proof", lambda *args: snapshot)
    actions = recovery.recover_lambda_application(
        run_root, sdk, proof, Path(proof["project"]), lambda *args: None, {}
    )
    assert any(a["outcome"] == "failed" and "Functions remain" in a["error"] for a in actions)
    assert not any(operation == "delete_log_group" for _, operation, _ in sdk.calls)


def test_report_preserves_all_seven_cases_and_phases_deterministically(run_root):
    for name in sorted(LAMBDA_TESTS):
        for phase in ("setup", "call", "teardown"):
            event(
                run_root,
                "test_result",
                stage="aws-lambda-vpc",
                nodeid=f"module::{name}",
                phase=phase,
                outcome="passed",
                duration=0.125,
            )
    report = generate(run_root)
    assert len(report["test_results"]) == 21
    text = (run_root / "report.md").read_text()
    assert all(name in text for name in LAMBDA_TESTS)
    assert "| teardown | passed | 0.125 |" in text
    assert generate(run_root) == report
    assert (run_root / "report.md").read_text() == text
