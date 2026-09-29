"""Unit tests for the local Lambda context mock used by `stlv dev`."""

from unittest.mock import patch

from pytest import mark, param

from stelvio.bridge.local.lambda_context import LambdaContext


def _context(**overrides):
    kwargs = {
        "invoke_id": "req-1",
        "client_context": None,
        "cognito_identity": None,
        "epoch_deadline_time_in_ms": 0,
        "invoked_function_arn": "arn:aws:lambda:us-east-1:123:function:test",
        "tenant_id": "tenant-1",
    }
    kwargs.update(overrides)
    return LambdaContext(**kwargs)


def test_kwargs_map_to_public_attributes(monkeypatch):
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_VERSION", "7")
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_MEMORY_SIZE", "256")
    monkeypatch.setenv("AWS_LAMBDA_LOG_GROUP_NAME", "/aws/lambda/my-fn")
    monkeypatch.setenv("AWS_LAMBDA_LOG_STREAM_NAME", "2026/01/01/[$LATEST]abc")

    ctx = _context()

    assert ctx.aws_request_id == "req-1"
    assert ctx.invoked_function_arn == "arn:aws:lambda:us-east-1:123:function:test"
    assert ctx.tenant_id == "tenant-1"
    assert ctx.function_name == "my-fn"
    assert ctx.function_version == "7"
    assert ctx.memory_limit_in_mb == "256"
    assert ctx.log_group_name == "/aws/lambda/my-fn"
    assert ctx.log_stream_name == "2026/01/01/[$LATEST]abc"
    assert ctx.client_context is None
    assert ctx.identity.cognito_identity_id is None
    assert ctx.identity.cognito_identity_pool_id is None


@mark.parametrize(
    ("deadline_offset_ms", "expected"),
    [
        param(-1, 0, id="past"),
        param(0, 0, id="exact-now"),
        param(30_000, 30_000, id="future-30s"),
    ],
)
def test_remaining_time_in_millis(deadline_offset_ms, expected):
    now = 1_700_000_000.0
    deadline_ms = int(now * 1000) + deadline_offset_ms
    ctx = _context(epoch_deadline_time_in_ms=deadline_ms)

    with patch("stelvio.bridge.local.lambda_context.time.time", return_value=now):
        assert ctx.get_remaining_time_in_millis() == expected


def test_cognito_identity_snake_case():
    ctx = _context(
        cognito_identity={
            "cognito_identity_id": "id-snake",
            "cognito_identity_pool_id": "pool-snake",
        }
    )

    assert ctx.identity.cognito_identity_id == "id-snake"
    assert ctx.identity.cognito_identity_pool_id == "pool-snake"


def test_client_context_nested_dict():
    ctx = _context(
        client_context={
            "custom": {"key": "value"},
            "env": {"platform": "iOS"},
            "client": {
                "installation_id": "install-1",
                "app_title": "App",
                "app_version_name": "1.0",
                "app_version_code": "10",
                "app_package_name": "com.example.app",
            },
        }
    )

    assert ctx.client_context.custom == {"key": "value"}
    assert ctx.client_context.env == {"platform": "iOS"}
    assert ctx.client_context.client.installation_id == "install-1"
    assert ctx.client_context.client.app_title == "App"
    assert ctx.client_context.client.app_version_name == "1.0"
    assert ctx.client_context.client.app_version_code == "10"
    assert ctx.client_context.client.app_package_name == "com.example.app"


def test_client_context_empty_dict():
    ctx = _context(client_context={})

    assert ctx.client_context.custom is None
    assert ctx.client_context.env is None
    assert ctx.client_context.client is None


def test_client_context_partial_custom_only():
    ctx = _context(client_context={"custom": {"key": "value"}})

    assert ctx.client_context.custom == {"key": "value"}
    assert ctx.client_context.env is None
    assert ctx.client_context.client is None


def test_client_context_client_none():
    ctx = _context(client_context={"custom": {"a": 1}, "client": None})

    assert ctx.client_context.custom == {"a": 1}
    assert ctx.client_context.client is None


def test_client_context_client_empty_dict():
    ctx = _context(client_context={"client": {}})

    assert ctx.client_context.client.installation_id is None
    assert ctx.client_context.client.app_title is None
    assert ctx.client_context.client.app_version_name is None
    assert ctx.client_context.client.app_version_code is None
    assert ctx.client_context.client.app_package_name is None


def test_log_writes_message_to_stdout(capsys):
    ctx = _context()

    ctx.log("hello from context")

    assert capsys.readouterr().out == "hello from context"
