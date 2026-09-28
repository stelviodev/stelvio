"""Unit tests for the local Lambda context mock used by `stlv dev`."""

import time

from pytest import mark, param

from stelvio.bridge.local.lambda_context import LambdaContext


def _context(**overrides):
    kwargs = {
        "invoke_id": "req-1",
        "client_context": None,
        "cognito_identity": None,
        "epoch_deadline_time_in_ms": None,
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


def test_remaining_time_future_deadline():
    future_ms = int(time.time() * 1000) + 30_000
    ctx = _context(epoch_deadline_time_in_ms=future_ms)

    remaining = ctx.get_remaining_time_in_millis()

    assert 0 < remaining <= 30_000


@mark.parametrize(
    "deadline",
    [
        param(None, id="none"),
        param(0, id="past-zero"),
        param(1, id="past-epoch"),
    ],
)
def test_remaining_time_none_or_past_returns_zero(deadline):
    ctx = _context(epoch_deadline_time_in_ms=deadline)

    assert ctx.get_remaining_time_in_millis() == 0


@mark.parametrize(
    ("cognito_identity", "expected_id", "expected_pool"),
    [
        param(
            {
                "cognito_identity_id": "id-snake",
                "cognito_identity_pool_id": "pool-snake",
            },
            "id-snake",
            "pool-snake",
            id="snake_case",
        ),
        param(
            {
                "cognitoIdentityId": "id-camel",
                "cognitoIdentityPoolId": "pool-camel",
            },
            "id-camel",
            "pool-camel",
            id="camelCase",
        ),
    ],
)
def test_cognito_identity_accepts_snake_and_camel(cognito_identity, expected_id, expected_pool):
    ctx = _context(cognito_identity=cognito_identity)

    assert ctx.identity.cognito_identity_id == expected_id
    assert ctx.identity.cognito_identity_pool_id == expected_pool


def test_client_context_none():
    ctx = _context(client_context=None)

    assert ctx.client_context is None


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

    assert ctx.client_context is not None
    assert ctx.client_context.custom == {"key": "value"}
    assert ctx.client_context.env == {"platform": "iOS"}
    assert ctx.client_context.client is not None
    assert ctx.client_context.client.installation_id == "install-1"
    assert ctx.client_context.client.app_package_name == "com.example.app"


def test_log_does_not_raise(capsys):
    ctx = _context()

    ctx.log("hello from context")

    assert "hello from context" in capsys.readouterr().out
