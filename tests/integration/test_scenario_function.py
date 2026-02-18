"""Scenario tests: Lambda function invocation.

Verifies Function URL responds to HTTP requests and that one Lambda can
invoke another through the Stelvio link system.
"""

import pytest

from stelvio.aws.function import Function

from .assert_helpers import http_request

pytestmark = pytest.mark.integration


def test_scenario_function_url(stelvio_env, project_dir):
    """HTTP POST to public function URL returns echoed body."""

    def infra():
        Function("responder", handler="handlers/echo.main", url="public")

    outputs = stelvio_env.deploy(infra)
    url = outputs["function_responder_url"]

    status, body = http_request(url, "POST", body={"hello": "world"})
    assert status == 200
    # echo handler returns {"statusCode": 200, "body": json.dumps(event)}
    # Function URL interprets this: HTTP 200, body = the JSON string
    assert "hello" in body


def test_scenario_function_invokes_function(stelvio_env, project_dir):
    """Caller Lambda invokes target Lambda via link and returns its response."""

    def infra():
        target = Function("target", handler="handlers/echo.main")
        Function(
            "caller",
            handler="handlers/invoker.main",
            url="public",
            links=[target],
        )

    outputs = stelvio_env.deploy(infra)
    caller_url = outputs["function_caller_url"]

    # Call the caller, which invokes the target with our payload
    status, body = http_request(caller_url, "POST", body={"msg": "from-caller"})
    assert status == 200
    # The response should contain our original payload (passed through invoker → target → echo)
    assert "from-caller" in body
