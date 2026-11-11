"""Non-AWS checks for the additive private-Lambda acceptance fixture."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pulumi
import pytest

from stelvio.app import StelvioApp
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc
from stelvio.component import ComponentRegistry
from tests.aws.pulumi_mocks import tid
from tests.integration import lambda_tunnel_session as fixture


class NotFoundError(Exception):
    pass


@pytest.fixture
def api(tmp_path):
    client = Mock()
    client.exceptions.NotFoundException = NotFoundError
    client.create_rest_api.return_value = {"id": "api123"}
    return fixture.PrivateApi(
        Mock(client=Mock(return_value=client)),
        "proof-app",
        "123456789012",
        "us-east-1",
        tmp_path / "api.json",
    )


def test_api_receipt_survives_later_failure(api):
    api.create()
    assert api.api_id == "api123"
    assert '"api_id": "api123"' in api.evidence.read_text()
    api.client.get_resources.side_effect = RuntimeError("integration failure")
    with pytest.raises(RuntimeError, match="integration failure"):
        api.configure("vpce-owned", "arn:aws:lambda:us-east-1:123456789012:function:owned")
    assert api.evidence.is_file()
    policy = api.client.update_rest_api.call_args.kwargs["patchOperations"][0]["value"]
    assert '"aws:SourceVpce": "vpce-owned"' in policy


def test_private_api_uses_publicly_resolvable_endpoint_alias(api):
    api.create()
    api.client.get_resources.return_value = {"items": [{"path": "/", "id": "root"}]}

    api.configure("vpce-owned", "arn:aws:lambda:us-east-1:123456789012:function:owned")

    assert api.url == "https://api123-vpce-owned.execute-api.us-east-1.amazonaws.com/proof/"
    assert api.client.update_rest_api.call_args.kwargs["patchOperations"][1] == {
        "op": "add",
        "path": "/endpointConfiguration/vpcEndpointIds",
        "value": "vpce-owned",
    }


def test_generated_lambda_app_uses_endpoint_alias_without_custom_domains(
    pulumi_mocks, monkeypatch
):
    source = Path(fixture.__file__).parent / "handlers/tunnel_lambda_app.py.txt"
    settings = {
        "app": "proof-app",
        "env": "proof",
        "region": "us-east-1",
        "account": "123456789012",
        "targets": [{"api_id": "api1"}, {"api_id": "api2"}],
    }
    exports = {}
    monkeypatch.setattr(StelvioApp, "_StelvioApp__instance", None)
    monkeypatch.setattr("stelvio.export_output", lambda key, value: exports.update({key: value}))
    monkeypatch.setattr(
        "pulumi_aws.ec2.VpcEndpoint",
        lambda name, **kwargs: SimpleNamespace(id=pulumi.Output.from_input(tid(name))),
    )
    monkeypatch.setattr(
        Vpc,
        "_create_resources",
        lambda self: SimpleNamespace(
            vpc=SimpleNamespace(id=self.name),
            bastion=SimpleNamespace(id=f"{self.name}-bastion"),
            bastion_security_group=SimpleNamespace(id=f"{self.name}-bastion-sg"),
            private_subnets=[SimpleNamespace(id=f"{self.name}-private")],
        ),
    )
    monkeypatch.setattr(
        Function,
        "_create_resources",
        lambda self: SimpleNamespace(
            function=SimpleNamespace(name=self.name),
            function_url=SimpleNamespace(function_url=f"https://{self.name}.example.test"),
        ),
    )

    @pulumi.runtime.test
    def deploy():
        namespace = {}
        exec(  # noqa: S102  # generated test-owned infrastructure with mocked providers
            compile(
                source.read_text().replace("SETTINGS_LITERAL", repr(settings)), str(source), "exec"
            ),
            namespace,
        )
        namespace["run"]()
        clients = {
            function.name: function for function in ComponentRegistry.instances_of(Function)
        }
        return pulumi.Output.all(
            exports["private_api1_url"],
            exports["private_api2_url"],
            clients["client1"].config.environment["PRIVATE_API_URL"],
            clients["client2"].config.environment["PRIVATE_API_URL"],
        ).apply(check)

    def check(values):
        expected = [
            f"https://api{number}-{tid(f'api-endpoint{number}')}.execute-api."
            "us-east-1.amazonaws.com/proof/"
            for number in (1, 2)
        ]
        assert values == expected * 2

    deploy()


@pytest.mark.parametrize(
    "record",
    [
        {"name": "other", "tags": {"stelvio:app": "proof-app", "stelvio:env": "proof"}},
        {"name": "proof-app", "tags": {"stelvio:app": "other", "stelvio:env": "proof"}},
        {"name": "proof-app", "tags": {"stelvio:app": "proof-app", "stelvio:env": "prod"}},
    ],
)
def test_foreign_api_is_never_deleted(api, record):
    api.create()
    api.client.get_rest_api.return_value = record
    with pytest.raises(RuntimeError, match="foreign API"):
        api.destroy()
    api.client.delete_rest_api.assert_not_called()


def test_owned_api_deletion_requires_independent_absence(api):
    api.create()
    api.client.get_rest_api.side_effect = [
        {"name": "proof-app", "tags": {"stelvio:app": "proof-app", "stelvio:env": "proof"}},
        NotFoundError(),
    ]
    api.destroy()
    api.client.delete_rest_api.assert_called_once_with(restApiId="api123")
    assert api.client.get_rest_api.call_count == 2


def test_absent_api_cleanup_is_idempotent(api):
    api.create()
    api.client.get_rest_api.side_effect = NotFoundError()
    api.destroy()
    api.client.delete_rest_api.assert_not_called()


@pytest.mark.parametrize("field", ["backend", "nonce", "sha256"])
def test_echo_assertion_rejects_wrong_backend_or_content(field):
    response = {"backend": "app", "nonce": "nonce", "sha256": hashlib.sha256(b"body").hexdigest()}
    fixture.assert_echo(response, app="app", nonce="nonce", payload="body")
    response[field] = "wrong"
    with pytest.raises(AssertionError):
        fixture.assert_echo(response, app="app", nonce="nonce", payload="body")


def test_wait_is_bounded(monkeypatch):
    clock = Mock(side_effect=[0, 0, 2])
    sleep = Mock()
    monkeypatch.setattr(fixture, "time", SimpleNamespace(monotonic=clock, sleep=sleep))
    with pytest.raises(TimeoutError, match="target"):
        fixture.wait_until(lambda: False, timeout=1, description="target")
    sleep.assert_called_once_with(1)


def test_wait_does_not_hide_api_errors():
    with pytest.raises(RuntimeError, match="credentials"):
        fixture.wait_until(
            Mock(side_effect=RuntimeError("credentials")), timeout=1, description="target"
        )


def test_app_cleanup_runs_after_api_cleanup_failure(tmp_path, monkeypatch):
    session = object.__new__(fixture.LambdaTunnelSession)
    session.project = tmp_path
    session.timings = {}
    session.apis = {1: Mock(), 2: Mock()}
    session.apis[1].destroy.side_effect = RuntimeError("API failure")
    cleanup = Mock()
    monkeypatch.setattr(fixture.StandaloneCliSession, "destroy", cleanup)
    session.write_evidence = Mock()
    with pytest.raises(ExceptionGroup, match="cleanup incomplete"):
        session.destroy()
    cleanup.assert_called_once()
    session.apis[2].destroy.assert_called_once()
    session.write_evidence.assert_called_once()


def test_private_http_client_disables_environment_proxies(monkeypatch):
    opener = MagicMock()
    response = opener.open.return_value.__enter__.return_value
    response.status = 200
    response.read.return_value = b'{"nonce":"proof"}'
    factory = Mock(return_value=opener)
    monkeypatch.setattr(fixture, "build_opener", factory)
    assert fixture.private_request("https://private.example/proof/", "proof", "body") == {
        "nonce": "proof"
    }
    assert factory.call_args.args[0].proxies == {}
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://private.example/proof/"
    assert request.get_method() == "POST"


def test_target_readiness_retries_connection_failure(monkeypatch):
    session = object.__new__(fixture.LambdaTunnelSession)
    session.check_private = Mock(side_effect=[fixture.URLError("TLS timeout"), {"ok": True}])
    monkeypatch.setattr(fixture, "time", SimpleNamespace(monotonic=lambda: 0, sleep=Mock()))
    assert session.wait_private("nonce") == {"ok": True}
    assert session.check_private.call_count == 2


def test_target_readiness_does_not_hide_wrong_response():
    session = object.__new__(fixture.LambdaTunnelSession)
    session.check_private = Mock(side_effect=AssertionError("wrong backend"))
    with pytest.raises(AssertionError, match="wrong backend"):
        session.wait_private("nonce")


@pytest.mark.parametrize("url", ["http://example.test/", "file:///etc/hosts"])
def test_private_request_requires_https(url):
    with pytest.raises(ValueError, match="HTTPS"):
        fixture.private_request(url, "nonce", "body")


def test_shared_app_captures_dev_logs_even_if_standalone_ran_last(monkeypatch):
    session = object.__new__(fixture.LambdaTunnelSession)
    session.aws = Mock()
    session.app = "proof-app"
    session.environment_name = "proof"
    session._mode = "standalone"
    session._proof_lambda_function_names = ("proof-app-proof-client", "proof-app-proof-echo")
    groups = ("/aws/lambda/proof-app-proof-client",)
    capture = Mock(return_value=groups)
    remove = Mock()
    monkeypatch.setattr(fixture, "capture_proof_lambda_log_groups", capture)
    monkeypatch.setattr(fixture, "delete_proof_lambda_log_groups", remove)
    assert session._capture_dev_lambda_log_groups() == (groups, [])
    assert session._remove_dev_lambda_log_groups(groups) == []
    assert remove.call_args.kwargs["function_names"] == session._proof_lambda_function_names
    assert remove.call_args.kwargs["log_group_names"] == groups


def test_throttle_retry_is_bounded_and_does_not_retry_unknown_outcomes(monkeypatch):
    throttle = fixture.ClientError(
        {"Error": {"Code": "TooManyRequestsException"}}, "DeleteRestApi"
    )
    operation = Mock(side_effect=throttle)
    sleep = Mock()
    monkeypatch.setattr(fixture, "time", SimpleNamespace(monotonic=lambda: 0, sleep=sleep))
    with pytest.raises(fixture.ClientError):
        fixture.retry_api_throttling(operation, restApiId="owned")
    assert operation.call_count == 10
    assert sleep.call_count == 9
    uncertain = Mock(side_effect=TimeoutError("response lost"))
    with pytest.raises(TimeoutError):
        fixture.retry_api_throttling(uncertain)
    uncertain.assert_called_once()


def test_throttle_retry_returns_success(monkeypatch):
    throttle = fixture.ClientError(
        {"Error": {"Code": "TooManyRequestsException"}}, "CreateRestApi"
    )
    operation = Mock(side_effect=[throttle, {"id": "owned"}])
    monkeypatch.setattr(fixture, "time", SimpleNamespace(monotonic=lambda: 0, sleep=Mock()))
    assert fixture.retry_api_throttling(operation) == {"id": "owned"}


def test_cross_vpc_backend_response_is_rejected():
    response = {
        "backend": "app:net2",
        "nonce": "nonce",
        "sha256": hashlib.sha256(b"x").hexdigest(),
    }
    with pytest.raises(AssertionError):
        fixture.assert_echo(response, app="app:net1", nonce="nonce", payload="x")


def test_dns_views_refuse_peer_probe_domain():
    session = object.__new__(fixture.LambdaTunnelSession)
    session.apis = {1: object(), 2: object()}
    session.network = lambda number: SimpleNamespace(identity=f"vpc-{number}")
    domains = {
        number: f"probe-{'1' * 32}-{hashlib.sha256(f'vpc-{number}'.encode()).hexdigest()[:8]}"
        ".stelvio.test"
        for number in (1, 2)
    }
    session.resolver_ports = Mock(return_value={domains[1]: 41001, domains[2]: 41002})
    session.wait_dns_rcode = Mock()
    session.dns_rcode = Mock(return_value=5)
    assert session.assert_dns_isolation() == session.resolver_ports.return_value
    assert session.dns_rcode.call_args_list == [
        ((domains[2], 41001),),
        ((domains[1], 41002),),
    ]
    assert all(call.args[2] == 3 for call in session.wait_dns_rcode.call_args_list)
    session.dns_rcode.return_value = 0
    with pytest.raises(AssertionError, match="Wrong VPC"):
        session.assert_dns_isolation()


def test_stopped_bastion_is_restored_after_failed_assertion():
    session = object.__new__(fixture.LambdaTunnelSession)
    session.account, session.app, session.environment_name = "123", "proof-app", "proof"
    session.network = Mock(
        return_value=SimpleNamespace(vpc_id="vpc-owned", bastion_instance_id="i-owned")
    )
    ec2 = Mock()
    ec2.describe_instances.return_value = {
        "Reservations": [
            {
                "OwnerId": "123",
                "Instances": [
                    {
                        "InstanceId": "i-owned",
                        "VpcId": "vpc-owned",
                        "State": {"Name": "running"},
                        "Tags": [
                            {"Key": "stelvio:app", "Value": "proof-app"},
                            {"Key": "stelvio:env", "Value": "proof"},
                        ],
                    }
                ],
            }
        ]
    }
    session.aws = Mock(client=Mock(return_value=ec2))
    with pytest.raises(AssertionError, match="fault assertion"), session.stopped_bastion(1):
        raise AssertionError("fault assertion")
    ec2.start_instances.assert_called_once_with(InstanceIds=["i-owned"])
    ec2.describe_instances.return_value["Reservations"][0]["OwnerId"] = "foreign"
    ec2.reset_mock()
    with pytest.raises(AssertionError), session.stopped_bastion(1):
        pass
    ec2.stop_instances.assert_not_called()
    ec2.start_instances.assert_not_called()


def test_deleted_endpoint_does_not_hide_surviving_peer(monkeypatch):
    session = object.__new__(fixture.LambdaTunnelSession)
    session.endpoint_ids = ["vpce-deleted", "vpce-live"]
    ec2 = Mock()
    ec2.describe_vpc_endpoints.side_effect = [
        fixture.ClientError(
            {"Error": {"Code": "InvalidVpcEndpointId.NotFound"}}, "DescribeVpcEndpoints"
        ),
        {"VpcEndpoints": [{"VpcEndpointId": "vpce-live"}]},
    ]
    session.aws = Mock(client=Mock(return_value=ec2))
    monkeypatch.setattr(fixture.StandaloneCliSession, "_assert_aws_resources_absent", Mock())

    def poll(check, **_kwargs):
        assert check() is False

    monkeypatch.setattr(fixture, "wait_until", poll)
    session._assert_aws_resources_absent()
    assert ec2.describe_vpc_endpoints.call_count == 2


def test_admission_rejection_requires_no_handler_entry():
    session = object.__new__(fixture.LambdaTunnelSession)
    session.network = Mock(return_value=SimpleNamespace(identity="vpc1"))
    session.invoke_function = Mock(
        return_value=(500, {"errorType": "VpcUnavailableError", "error": "vpc1 unavailable"})
    )
    session.invocations = Mock(side_effect=[[], [{"target": "1", "kind": "invoke"}]])
    with pytest.raises(AssertionError, match="handler ran"):
        session.wait_admission_rejected(1)


def test_parallel_access_keeps_sdk_probes_on_owner_thread(monkeypatch):
    from threading import Barrier, get_ident

    from tests.integration import test_tunnel_lambda as acceptance

    owner = get_ident()
    ready = Barrier(2, timeout=5)
    session = Mock(spec=fixture.LambdaTunnelSession)
    session.apis = {
        number: SimpleNamespace(url=f"https://api{number}.example") for number in (1, 2)
    }
    session.backend_identity.side_effect = lambda number: f"app:net{number}"
    session.host_pins.return_value = {"vpc1": "pin1", "vpc2": "pin2"}
    session.assert_dns_isolation.return_value = {"api1": 41001, "api2": 41002}

    def sdk_probe(_nonce, _number):
        assert get_ident() == owner, "boto3 Session was used in a worker thread"

    def http_client(url, nonce, payload):
        assert get_ident() != owner
        ready.wait()
        number = 1 if url == "https://api1.example" else 2
        return {
            "backend": f"app:net{number}",
            "nonce": nonce,
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        }

    session.wait_private.side_effect = sdk_probe
    client = Mock(side_effect=http_client)
    monkeypatch.setattr(acceptance, "private_request", client)
    assert acceptance.assert_two_vpc_access(session) == session.assert_dns_isolation.return_value
    assert session.wait_private.call_count == 2
    assert client.call_count == 2
    assert len({call.args[1] for call in client.call_args_list}) == 2
    assert len({call.args[2] for call in client.call_args_list}) == 2
    assert all(len(call.args[2]) >= 16384 for call in client.call_args_list)


@pytest.mark.parametrize("bad_field", [None, "plain", "app", "nonce", "local", "pid"])
def test_plain_clients_require_current_local_invocations(bad_field):
    session = object.__new__(fixture.LambdaTunnelSession)
    disabled = SimpleNamespace(identity="aws:net3", bastion_instance_id=None)
    session._manifest = SimpleNamespace(vpcs=[disabled])
    session.app = "proof-app"
    session.log = Mock(return_value="VPC access disabled: aws:net3; local networking")
    entries = []

    def invoke(number, *, marker):
        entries.append({"target": str(number), "nonce": marker, "pid": 123})
        response = {"plain": True, "app": session.app, "nonce": marker, "local": True, "pid": 123}
        if bad_field:
            response[bad_field] = False
        return 200, response

    session.invoke_function = Mock(side_effect=invoke)
    session.invocations = Mock(side_effect=lambda: entries)
    if bad_field:
        with pytest.raises(AssertionError):
            session.check_plain_clients()
    else:
        session.check_plain_clients()
        assert [call.args[0] for call in session.invoke_function.call_args_list] == [3, 4]


def test_plain_clients_require_one_disabled_vpc_warning():
    session = object.__new__(fixture.LambdaTunnelSession)
    session._manifest = SimpleNamespace(
        vpcs=[SimpleNamespace(identity="aws:net3", bastion_instance_id=None)]
    )
    session.log = Mock(return_value="VPC access disabled: aws:net3\n" * 2)
    session.invoke_function = Mock()
    with pytest.raises(AssertionError):
        session.check_plain_clients()
    session.invoke_function.assert_not_called()
