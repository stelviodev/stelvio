import pulumi
import pytest

from stelvio.aws.api_gateway.http_api import HttpApi
from stelvio.aws.function import Function, FunctionConfig

from .conftest import when_http_api_ready

pytestmark = pytest.mark.usefixtures("project_cwd")


@pytest.mark.parametrize(
    ("handler", "opts"),
    [
        # String handler + function options
        ("functions/simple.handler", {"memory": 512, "timeout": 60}),
        # FunctionConfig instance
        (FunctionConfig(handler="functions/simple.handler", memory=512, timeout=60), {}),
        # Plain dict
        ({"handler": "functions/simple.handler", "memory": 512, "timeout": 60}, {}),
    ],
    ids=["string_handler_and_opts", "function_config", "dict"],
)
@pulumi.runtime.test
def test_http_api_route_handler_configuration(pulumi_mocks, handler, opts):
    api = HttpApi("my-api")
    api.route("GET", "/users", handler, **opts)
    _ = api.resources

    def check(_):
        functions = pulumi_mocks.created_functions()

        assert len(functions) == 1
        assert functions[0].typ == "aws:lambda/function:Function"
        assert functions[0].name == "test-test-my-api-functions-simple_handler"
        assert functions[0].inputs["memorySize"] == 512
        assert functions[0].inputs["timeout"] == 60

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_route_uses_supplied_function(pulumi_mocks):
    function = Function("users-function", handler="functions/users.handler")
    api = HttpApi("my-api")
    api.route("GET", "/users", function)
    _ = api.resources

    def check(_):
        functions = pulumi_mocks.created_functions()

        assert len(functions) == 1
        assert functions[0].name == "test-test-users-function"

        permissions = pulumi_mocks.created_permissions()
        assert len(permissions) == 1
        assert permissions[0].inputs["function"] == "test-test-users-function-test-name"

    when_http_api_ready(api, check)


@pytest.mark.parametrize(
    ("handler", "opts", "expected_error"),
    [
        (
            None,
            {},
            "Missing handler configuration: when handler argument is None, "
            "'handler' option must be provided",
        ),
        (
            "functions/simple.handler",
            {"handler": "functions/users.handler"},
            "Ambiguous handler configuration",
        ),
        (
            {"handler": "functions/simple.handler"},
            {"memory": 256},
            "Invalid configuration: cannot combine complete handler configuration",
        ),
    ],
)
def test_http_api_route_rejects_invalid_handler_configuration(handler, opts, expected_error):
    api = HttpApi("my-api")

    with pytest.raises(ValueError, match=expected_error):
        api.route("GET", "/users", handler, **opts)


def test_http_api_route_rejects_invalid_handler_type():
    api = HttpApi("my-api")

    with pytest.raises(TypeError, match="Invalid handler type: int"):
        api.route("GET", "/users", 123)  # type: ignore[arg-type]


@pulumi.runtime.test
def test_http_api_rejects_route_changes_after_resources_are_created(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with pytest.raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
        api.route("POST", "/users", "functions/users.handler")


@pulumi.runtime.test
def test_http_api_rejects_authorizer_changes_after_resources_are_created(pulumi_mocks):
    api = HttpApi("my-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with pytest.raises(RuntimeError, match="Cannot modify HttpApi 'my-api'"):
        api.add_jwt_authorizer(
            "auth",
            issuer="https://issuer.example.com",
            audiences=["client-id"],
        )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        # Two string-handler routes disagreeing on memory/timeout
        (
            ("GET", "/users", "functions/simple.handler", {"memory": 256}),
            ("POST", "/users", "functions/simple.handler", {"timeout": 30}),
        ),
        # FunctionConfig + string-handler disagreeing
        (
            ("GET", "/users", FunctionConfig(handler="functions/simple.handler", memory=256)),
            ("POST", "/users", "functions/simple.handler", {"timeout": 30}),
        ),
    ],
    ids=["string_handlers", "mixed_function_config"],
)
@pulumi.runtime.test
def test_http_api_conflicting_lambda_configurations_raise(pulumi_mocks, first, second):
    api = HttpApi("my-api")
    api.route(first[0], first[1], first[2], **(first[3] if len(first) > 3 else {}))
    api.route(second[0], second[1], second[2], **(second[3] if len(second) > 3 else {}))

    with pytest.raises(
        ValueError, match="Multiple routes try to configure the same Lambda function"
    ):
        _ = api.resources
