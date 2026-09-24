import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Literal

import pulumi
from pytest import mark, raises, warns

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.api_gateway.rest_api.config import RestApiConfig
from stelvio.aws.api_gateway.rest_api.constants import API_GATEWAY_ROLE_NAME
from stelvio.aws.function import Function, FunctionConfig

from ....conftest import TP
from ...conftest import assert_hash_truncated, spy_old_names
from ...pulumi_mocks import (
    ACCOUNT_ID,
    DEFAULT_REGION,
    ROOT_RESOURCE_ID,
    SAMPLE_API_ID,
    PulumiTestMocks,
    R,
    tid,
    tn,
)

pytestmark = mark.usefixtures("project_cwd")


# API resources (path parts)
class PathPart:
    USERS: str = "users"
    USER_ID: str = "{userId}"
    ORDERS: str = "orders"
    ORDER_ID: str = "{orderId}"
    ITEMS: str = "items"
    REPORT: str = "report"


API_NAME = "test-api"


@dataclass
class Func:
    handler: str
    name: str
    timeout: int | None = None
    memory: int | None = None
    instance: Function | None = None

    def full_name(self, api_name: str) -> str:
        """String handlers get a Lambda named after the API; an instance keeps its own name."""
        return self.name if self.instance else f"{api_name}-{self.name}"

    @property
    def lambda_handler(self) -> str:
        """`functions/folder::handler.fn` deploys as `handler.fn`, `functions/simple.handler`
        as `simple.handler`: the packaged file plus the function name."""
        return self.handler.split("::")[-1].rsplit("/", 1)[-1]


class Funcs:
    # Single-file functions
    SIMPLE: Func = Func("functions/simple.handler", "functions-simple_handler")
    SIMPLE2: Func = Func("functions/simple2.handler", "functions-simple2_handler")
    USERS: Func = Func("functions/users.handler", "functions-users_handler")
    ORDERS: Func = Func("functions/orders.handler", "functions-orders_handler")

    FOLDER_HANDLER: Func = Func("functions/folder::handler.fn", "functions-folder-handler_fn")
    FOLDER_HANDLER2: Func = Func("functions/folder::handler2.fn", "functions-folder-handler2_fn")
    FOLDER2_HANDLER: Func = Func("functions/folder2::handler.fn", "functions-folder2-handler_fn")
    FOLDER_PROCESS: Func = Func(
        "functions/folder::handler.process", "functions-folder-handler_process"
    )
    FOLDER2_PROCESS: Func = Func(
        "functions/folder2::handler.process", "functions-folder2-handler_process"
    )


@dataclass
class Method:
    verb: Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "ANY"]
    function: Func


@dataclass
class Res:
    path_part: str
    methods: list[Method] = field(default_factory=list)
    children: list["Res"] = field(default_factory=list)

    def full_path_parts(self, parent_parts: list[str]):
        return [*parent_parts, self.path_part or "root"]

    def path(self, parent_parts: list[str]) -> str:
        return "/" + "/".join(p for p in [*parent_parts, self.path_part] if p)

    def name(self, api_name, parent_parts: list[str]):
        return f"{api_name}-resource-{self.path(parent_parts)}"


# route() validation and conflicts: test_api_route.py; the deployment trigger hash:
# test_api_deployment.py. Here: the resource graph one RestApi deploys.

LAMBDA_INVOKE_ARN_TEMPLATE = (
    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/"
    f"functions/arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:"
    f"function:{{function_name}}/invocations"
)
API_EXECUTION_ARN = f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{SAMPLE_API_ID}"
API_INVOKE_SOURCE_ARN = f"{API_EXECUTION_ARN}/*/*"
ACCESS_LOG_FORMAT = (
    '{"requestId":"$context.requestId", "ip": "$context.identity.sourceIp", '
    '"caller":"$context.identity.caller", "user":"$context.identity.user",'
    '"requestTime":"$context.requestTime", "httpMethod":'
    '"$context.httpMethod","resourcePath":"$context.resourcePath", '
    '"status":"$context.status","protocol":"$context.protocol", '
    '"responseLength":"$context.responseLength"}'
)

API_GATEWAY_ASSUME_ROLE_POLICY = [
    {
        "actions": ["sts:AssumeRole"],
        "principals": [{"identifiers": ["apigateway.amazonaws.com"], "type": "Service"}],
    }
]


def rest_api_counts(functions: int, resources: int, methods: int, *, apis: int = 1) -> Counter[R]:
    """Resource counts of a program with `apis` REST APIs and `functions` Lambdas in total.

    The account (a read plus the managed one) and its role are created once per program.
    """
    return +Counter(
        {
            R.REST_API: apis,
            R.LOG_GROUP: apis,
            R.API_DEPLOYMENT: apis,
            R.API_STAGE: apis,
            R.API_ACCOUNT: 2,
            R.ROLE: 1 + functions,
            R.ROLE_POLICY_ATTACHMENT: functions,
            R.FUNCTION: functions,
            R.LAMBDA_PERMISSION: functions,
            R.API_RESOURCE: resources,
            R.API_METHOD: methods,
            R.API_INTEGRATION: methods,
        }
    )


def structure_counts(api_structure: list[Res]) -> tuple[int, int]:
    """(non-root resources, methods) a structure deploys."""
    resources = methods = 0
    for res in api_structure:
        child_resources, child_methods = structure_counts(res.children)
        resources += bool(res.path_part) + child_resources
        methods += len(res.methods) + child_methods
    return resources, methods


def assert_rest_api(mocks: PulumiTestMocks, name: str, expected_endpoint_type: str = "REGIONAL"):
    mocks.assert_res(
        name, R.REST_API, {"endpointConfiguration": {"types": expected_endpoint_type}}
    )


def assert_api_account_and_role(mocks: PulumiTestMocks):
    mocks.assert_res(
        API_GATEWAY_ROLE_NAME,
        R.ROLE,
        {
            "assumeRolePolicy": json.dumps(API_GATEWAY_ASSUME_ROLE_POLICY),
            "managedPolicyArns": [
                "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
            ],
        },
        prefixed=False,
    )
    mocks.assert_res(
        "api-gateway-account",
        R.API_ACCOUNT,
        {"cloudwatchRoleArn": f"arn:aws:iam::{ACCOUNT_ID}:role/{tn(API_GATEWAY_ROLE_NAME)}"},
        prefixed=False,
    )


def assert_functions(mocks: PulumiTestMocks, functions: list[Func], api_name: str):
    for fn in functions:
        mocks.assert_res(
            fn.full_name(api_name),
            R.FUNCTION,
            {
                "handler": fn.lambda_handler,
                "memorySize": fn.memory or 128,
                "timeout": fn.timeout or 60,
            },
            partial=True,
        )


def assert_permission(mocks: PulumiTestMocks, function: Func, api_name: str):
    name = function.full_name(api_name)
    mocks.assert_res(
        f"{api_name}-permission-{name}",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunction",
            "function": tn(TP + name),
            "principal": "apigateway.amazonaws.com",
            "sourceArn": API_INVOKE_SOURCE_ARN,
        },
    )


def assert_resources_methods_and_integrations(
    mocks: PulumiTestMocks,
    api_name: str,
    api_structure: list[Res],
    parent_parts: list[str] | None = None,
    parent_id: str = ROOT_RESOURCE_ID,
):
    parent_parts = parent_parts or []
    api_id = tid(TP + api_name)
    for res in api_structure:
        path = res.path(parent_parts)
        if res.path_part:
            name = res.name(api_name, parent_parts)
            mocks.assert_res(
                name,
                R.API_RESOURCE,
                {"restApi": api_id, "parentId": parent_id, "pathPart": res.path_part},
            )
            resource_id = tid(TP + name)
        else:
            resource_id = ROOT_RESOURCE_ID
        for method in res.methods:
            mocks.assert_res(
                f"{api_name}-method-{method.verb} {path}",
                R.API_METHOD,
                {
                    "restApi": api_id,
                    "resourceId": resource_id,
                    "httpMethod": method.verb,
                    "authorization": "NONE",
                },
            )
            mocks.assert_res(
                f"{api_name}-integration-{method.verb} {path}",
                R.API_INTEGRATION,
                {
                    "restApi": api_id,
                    "resourceId": resource_id,
                    "httpMethod": method.verb,
                    "integrationHttpMethod": "POST",
                    "type": "AWS_PROXY",
                    "uri": LAMBDA_INVOKE_ARN_TEMPLATE.format(
                        function_name=tn(TP + method.function.full_name(api_name))
                    ),
                },
            )
        assert_resources_methods_and_integrations(
            mocks, api_name, res.children, res.full_path_parts(parent_parts), resource_id
        )


def assert_deployment(mocks: PulumiTestMocks, api_name: str):
    deployment = mocks.assert_res(
        f"{api_name}-deployment", R.API_DEPLOYMENT, {"restApi": tid(TP + api_name)}, partial=True
    )
    # The hash value is pinned in test_api_deployment.py.
    assert re.fullmatch(r"[0-9a-f]{64}", deployment.inputs["triggers"]["configuration_hash"])


def assert_stage(mocks: PulumiTestMocks, api_name: str, expected_stage_name: str = "v1"):
    mocks.assert_res(
        f"{api_name}-logs",
        R.LOG_GROUP,
        {"name": f"/aws/apigateway/{tn(TP + api_name)}", "retentionInDays": 30},
    )
    mocks.assert_res(
        f"{api_name}-stage-{expected_stage_name}",
        R.API_STAGE,
        {
            "restApi": tid(TP + api_name),
            "deployment": tid(f"{TP}{api_name}-deployment"),
            "stageName": expected_stage_name,
            "accessLogSettings": {
                "destinationArn": (
                    f"arn:aws:logs:{DEFAULT_REGION}:{ACCOUNT_ID}:log-group:"
                    f"{tn(f'{TP}{api_name}-logs')}:*"
                ),
                "format": ACCESS_LOG_FORMAT,
            },
            "variables": {"loggingLevel": "INFO"},
        },
    )


def assert_api_gateway_resources(  # noqa: PLR0913
    mocks: PulumiTestMocks,
    api_name: str,
    api_structure: list[Res],
    expected_functions: list[Func],
    expected_endpoint_type: str = "REGIONAL",
    expected_stage_name: str = "v1",
    *,
    seal: bool = True,
):
    """The whole graph of one API. `seal=False` when the program holds more than one API."""
    assert_rest_api(mocks, api_name, expected_endpoint_type)
    assert_api_account_and_role(mocks)
    assert_functions(mocks, expected_functions, api_name)
    assert_resources_methods_and_integrations(mocks, api_name, api_structure)
    assert_deployment(mocks, api_name)
    assert_stage(mocks, api_name, expected_stage_name)
    for function in expected_functions:
        assert_permission(mocks, function, api_name)
    if seal:
        resources, methods = structure_counts(api_structure)
        mocks.assert_res_counts(rest_api_counts(len(expected_functions), resources, methods))


@pulumi.runtime.test
def test_rest_api_properties(pulumi_mocks):
    """Test that RestApi.resources property correctly provides access to created resources."""
    api = RestApi("test-api")
    api.route("GET", "/users", "functions/simple.handler")

    # Create the resource
    _ = api.resources

    def check_resources(args):
        rest_api_id, stage_id, deployment_id, api_arn, url = args

        # Verify resource IDs match expected patterns
        assert rest_api_id == TP + "test-api-test-id"
        assert stage_id == TP + "test-api-stage-v1-test-id"
        assert deployment_id == TP + "test-api-deployment-test-id"

        # Check that convenience properties have expected formats
        assert api_arn == "arn:aws:apigateway:us-east-1::/restapis/12345abcde"
        expected_url = f"https://{TP}test-api-test-id.execute-api.us-east-1.amazonaws.com/v1"
        assert url == expected_url

    return pulumi.Output.all(
        api.resources.rest_api.id,
        api.resources.stage.id,
        api.resources.deployment.id,
        api.arn,
        api.url,
    ).apply(check_resources)


@pulumi.runtime.test
def test_rest_api_invoke_url_alias_warns(pulumi_mocks):
    api = RestApi("test-api")
    api.route("GET", "/users", "functions/simple.handler")
    with warns(DeprecationWarning, match="invoke_url is deprecated"):
        _ = api.invoke_url


def test_rest_api_link_injects_api_url_env_vars(pulumi_mocks):
    api = RestApi("orders-api")
    api.route("GET", "/orders", "functions/simple.handler")
    fn = Function("client", handler="functions/simple.handler", links=[api])

    @pulumi.runtime.test
    def deploy():
        return [fn.resources, api.resources]

    deploy()

    client = pulumi_mocks.assert_res("client", R.FUNCTION)
    assert client.inputs["environment"]["variables"] == {
        "STLV_ORDERS_API_API_URL": (
            f"https://{tid(TP + 'orders-api')}.execute-api.{DEFAULT_REGION}.amazonaws.com/v1"
        ),
        "STLV_ORDERS_API_API_EXECUTION_ARN": API_EXECUTION_ARN,
    }


def test_rest_api_url_with_domain_allows_adding_routes_after(pulumi_mocks, app_context_with_dns):
    # With a domain the url is known upfront, so reading it must not create resources —
    # otherwise passing api.url to another component locks the api before all routes
    # are added. Adding a route after is what fails if the fallback is evaluated eagerly.
    api = RestApi("lazy-url-api", domain_name="api.example.com")
    url = api.url
    api.route("GET", "/users", "functions/simple.handler")

    def check_url(resolved):
        assert resolved == "https://api.example.com"

    @pulumi.runtime.test
    def deploy():
        _ = api.resources
        return url.apply(check_url)

    deploy()

    pulumi_mocks.assert_res("lazy-url-api-method-GET /users", R.API_METHOD)


def test_rest_api_custom_domain_base_path(pulumi_mocks, app_context_with_dns):
    api = RestApi("test-api", domain_name="api.example.com", base_path="v1")
    api.route("GET", "/users", "functions/simple.handler")

    def check_url(url):
        assert url == "https://api.example.com/v1"

    @pulumi.runtime.test
    def deploy():
        _ = api.resources
        return api.url.apply(check_url)

    deploy()

    pulumi_mocks.assert_res(
        "test-api-custom-domain-base-path-mapping",
        R.API_BASE_PATH_MAPPING,
        {
            "restApi": tid(TP + "test-api"),
            "stageName": "v1",
            "domainName": "api.example.com",
            "basePath": "v1",
        },
    )


def test_rest_api_invalid_access_log_retention_days():
    with raises(ValueError, match="access_log_retention_days"):
        RestApi("test-api", access_log_retention_days=999)


def test_rest_api_root(pulumi_mocks):
    """0. Basic API Resource Creation:
    - Only root / route with GET method and simple handler
    """
    # Arrange
    api = RestApi(API_NAME)
    api.route("GET", "/", Funcs.SIMPLE.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    api_structure = [Res(None, [Method("GET", Funcs.SIMPLE)])]
    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, [Funcs.SIMPLE])


def test_rest_api_basic(pulumi_mocks):
    """1. Basic API Resource Creation:
    - Single route with GET method and simple handler
    """
    # Arrange
    api = RestApi(API_NAME)
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    api_structure = [Res(PathPart.USERS, [Method("GET", Funcs.SIMPLE)])]
    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, [Funcs.SIMPLE])


def test_api_resources_multiple_paths(pulumi_mocks):
    """2. Multiple Routes:
    - Routes with multiple paths ("/users", "/users/{id}", "/orders")
    - Verify correct resource creation for each path
    - Verify parent-child relationships between resources
    """
    # Arrange - create an API with multiple simple routes
    api = RestApi(API_NAME)
    api.route("GET", f"/{PathPart.USERS}", Funcs.USERS.handler)
    # Add a simple parameter route
    api.route("GET", f"/{PathPart.USERS}/{PathPart.USER_ID}", Funcs.USERS.handler)

    api.route("GET", f"/{PathPart.ORDERS}", Funcs.ORDERS.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    api_structure = [
        Res(
            PathPart.USERS,
            [Method("GET", Funcs.USERS)],
            children=[Res(PathPart.USER_ID, [Method("GET", Funcs.USERS)])],
        ),
        Res(PathPart.ORDERS, [Method("GET", Funcs.ORDERS)]),
    ]
    assert_api_gateway_resources(
        pulumi_mocks, API_NAME, api_structure, [Funcs.USERS, Funcs.ORDERS]
    )


def test_api_path_parameter_handling(pulumi_mocks):
    """3. Path Parameter Handling:
    - Simple parameters ("/users/{id}")
    - Deep nested parameters ("/users/{userId}/orders/{orderId}")
    - Greedy parameters ("/files/{proxy+}")
    - Multi-segment paths ("/api/v1/resources")
    """
    # Arrange
    api = RestApi(API_NAME)
    # Simple parameters
    api.route("GET", f"/{PathPart.USERS}/{PathPart.USER_ID}", Funcs.USERS.handler)
    # Deep nested parameters
    api.route(
        "GET",
        f"/{PathPart.USERS}/{PathPart.USER_ID}/{PathPart.ORDERS}/{PathPart.ORDER_ID}",
        Funcs.ORDERS.handler,
    )

    # Greedy parameter
    api.route("GET", "/files/{proxy+}", Funcs.SIMPLE.handler)

    # Multi-segment path
    api.route("GET", "/api/v1/resources", Funcs.SIMPLE.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    # Define API structure using the Res dataclass
    api_structure = [
        Res(  # /users/{userId}
            PathPart.USERS,
            children=[
                Res(
                    PathPart.USER_ID,
                    [Method("GET", Funcs.USERS)],
                    children=[
                        # /users/{userId}/orders/{orderId}
                        Res(
                            PathPart.ORDERS,
                            children=[Res(PathPart.ORDER_ID, [Method("GET", Funcs.ORDERS)])],
                        )
                    ],
                )
            ],
        ),
        # /files/{proxy+}
        Res("files", children=[Res("{proxy+}", [Method("GET", Funcs.SIMPLE)])]),
        Res(  # /api/v1/resources
            "api",
            children=[Res("v1", [], children=[Res("resources", [Method("GET", Funcs.SIMPLE)])])],
        ),
    ]
    assert_api_gateway_resources(
        pulumi_mocks,
        API_NAME,
        api_structure,
        [Funcs.USERS, Funcs.ORDERS, Funcs.SIMPLE],
    )


@mark.parametrize(
    ("route_style", "include_any_method"),
    [
        ("individual", False),  # Individual calls to api.route for each HTTP method
        ("list", False),  # Single call with list of HTTP methods
        ("individual", True),  # With ANY method on second path
        ("list", True),  # With ANY method on second path
    ],
    ids=["individual", "list", "individual_with_any", "list_with_any"],
)
def test_http_method_handling(pulumi_mocks, route_style, include_any_method):
    """4. HTTP Method Handling:
    - Multiple methods on single route (GET, POST, PUT, DELETE on "/users")
    - ANY method for catch-all endpoint ("/reports")
    - Verify method resources and integrations for each HTTP method
    - Test both individual method routing and list-based method routing
    """
    # Arrange
    api = RestApi(API_NAME)

    # Configure multiple standard HTTP methods on users route
    if route_style == "individual":
        # Individual route calls for each method
        api.route("GET", f"/{PathPart.USERS}", Funcs.USERS.handler)
        api.route("POST", f"/{PathPart.USERS}", Funcs.USERS.handler)
        api.route("PUT", f"/{PathPart.USERS}", Funcs.USERS.handler)
        api.route("DELETE", f"/{PathPart.USERS}", Funcs.USERS.handler)
        # Add orders path with single method
        api.route("GET", f"/{PathPart.ORDERS}", Funcs.ORDERS.handler)
    else:
        # Single route call with list of methods
        api.route(["GET", "POST", "PUT", "DELETE"], f"/{PathPart.USERS}", Funcs.USERS.handler)
        api.route("GET", f"/{PathPart.ORDERS}", Funcs.ORDERS.handler)
        # Add ANY method for reports if required
    if include_any_method:
        api.route("ANY", f"/{PathPart.REPORT}", Funcs.SIMPLE.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    # Define expected API structure
    api_structure = [
        Res(
            PathPart.USERS,
            [
                Method("GET", Funcs.USERS),
                Method("POST", Funcs.USERS),
                Method("PUT", Funcs.USERS),
                Method("DELETE", Funcs.USERS),
            ],
        ),
        Res(PathPart.ORDERS, [Method("GET", Funcs.ORDERS)]),
    ]
    expected_functions = [Funcs.USERS, Funcs.ORDERS]

    # Add report resource with ANY method if required
    if include_any_method:
        api_structure.append(Res(PathPart.REPORT, [Method("ANY", Funcs.SIMPLE)]))
        expected_functions.append(Funcs.SIMPLE)

    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, expected_functions)


def test_function_instance_handler_configuration(pulumi_mocks):
    """5. Handler Configuration Types - Function Object:
       - Test Function object passed to route

    Verifies that when a Function object is provided to api.route(),
    the existing function is reused rather than creating a new one.
    """
    # Arrange
    api = RestApi(API_NAME)

    custom_function = Function(
        "test-custom-function", FunctionConfig(handler=Funcs.USERS.handler, memory=256, timeout=60)
    )

    # Configure route with Function object
    api.route("GET", f"/{PathPart.USERS}", custom_function)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    expected_function = Func(
        custom_function.config.handler,
        custom_function.name,
        timeout=custom_function.config.timeout,
        memory=custom_function.config.memory,
        instance=custom_function,
    )
    api_structure = [Res(PathPart.USERS, [Method("GET", expected_function)])]
    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, [expected_function])


@mark.parametrize(
    ("args", "kwargs"),
    [
        ([], {"handler": Funcs.USERS.handler, "memory": 256, "timeout": 60}),
        ([FunctionConfig(handler=Funcs.USERS.handler, memory=256, timeout=60)], {}),
        ([{"handler": Funcs.USERS.handler, "memory": 256, "timeout": 60}], {}),
    ],
    ids=["string_handler_and_opts", "function_config_handler", "function_config_dict_handler"],
)
def test_route_handler_configuration__(pulumi_mocks, args, kwargs):
    # Arrange
    api = RestApi(API_NAME)
    api.route("GET", f"/{PathPart.USERS}", *args, **kwargs)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    expected_fn = replace(Funcs.USERS, timeout=60, memory=256)
    api_structure = [Res(PathPart.USERS, [Method("GET", expected_fn)])]
    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, [expected_fn])


def test_lambda_function_reuse_single_file(pulumi_mocks):
    # Arrange
    api = RestApi(API_NAME)

    # Configure multiple routes pointing to the same single-file function
    api.route("GET", f"/{PathPart.USERS}", Funcs.USERS.handler)
    api.route("POST", f"/{PathPart.USERS}", Funcs.USERS.handler)  # Same handler
    # Same handler with different path
    api.route("PUT", f"/{PathPart.USERS}/{PathPart.USER_ID}", Funcs.USERS.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    api_structure = [
        Res(
            PathPart.USERS,
            [Method("GET", Funcs.USERS), Method("POST", Funcs.USERS)],
            children=[Res(PathPart.USER_ID, [Method("PUT", Funcs.USERS)])],
        )
    ]
    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, [Funcs.USERS])


def test_configured_route_sets_shared_lambda_config_regardless_of_order(pulumi_mocks):
    """Routes on one handler share a Lambda; the route that configures it wins even when a
    plain route on the same handler was declared first."""
    api = RestApi(API_NAME)
    api.route("GET", f"/{PathPart.USERS}", Funcs.USERS.handler)
    api.route("POST", f"/{PathPart.USERS}", Funcs.USERS.handler, memory=256)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    fn = replace(Funcs.USERS, memory=256)
    api_structure = [Res(PathPart.USERS, [Method("GET", fn), Method("POST", fn)])]
    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, [fn])


def test_lambda_function_separate_single_files(pulumi_mocks):
    # Arrange
    api = RestApi(API_NAME)

    # Configure routes pointing to different single-file functions
    api.route("GET", f"/{PathPart.USERS}", Funcs.USERS.handler)
    api.route("GET", f"/{PathPart.ORDERS}", Funcs.ORDERS.handler)
    api.route("GET", f"/{PathPart.ITEMS}", Funcs.SIMPLE.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    api_structure = [
        Res(PathPart.USERS, [Method("GET", Funcs.USERS)]),
        Res(PathPart.ORDERS, [Method("GET", Funcs.ORDERS)]),
        Res("items", [Method("GET", Funcs.SIMPLE)]),
    ]

    assert_api_gateway_resources(
        pulumi_mocks,
        API_NAME,
        api_structure,
        [Funcs.USERS, Funcs.ORDERS, Funcs.SIMPLE],
    )


def test_lambda_function_reuse_folder_based(pulumi_mocks):
    # Arrange
    api = RestApi(API_NAME)

    # Configure routes pointing to different handlers but in the same folder function
    api.route("GET", "/folder/handler", Funcs.FOLDER_HANDLER.handler)
    api.route("POST", "/folder/handler2", Funcs.FOLDER_HANDLER2.handler)
    api.route("PUT", "/folder/handler2", Funcs.FOLDER_HANDLER2.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    api_structure = [
        Res(
            "folder",
            children=[
                Res("handler", [Method("GET", Funcs.FOLDER_HANDLER)]),
                Res(
                    "handler2",
                    [
                        Method("POST", Funcs.FOLDER_HANDLER2),
                        Method("PUT", Funcs.FOLDER_HANDLER2),
                    ],
                ),
            ],
        )
    ]
    # Pass the expected functions (both should point to the same Lambda)
    assert_api_gateway_resources(
        pulumi_mocks,
        API_NAME,
        api_structure,
        [Funcs.FOLDER_HANDLER, Funcs.FOLDER_HANDLER2],
    )


def test_lambda_function_separate_folder_based(pulumi_mocks):
    # Arrange
    api = RestApi(API_NAME)

    # Configure routes pointing to different folder-based functions
    api.route("GET", "/folder/handler", Funcs.FOLDER_HANDLER.handler)
    api.route("GET", "/folder2/handler", Funcs.FOLDER2_HANDLER.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    api_structure = [
        Res("folder", children=[Res("handler", [Method("GET", Funcs.FOLDER_HANDLER)])]),
        Res("folder2", children=[Res("handler", [Method("GET", Funcs.FOLDER2_HANDLER)])]),
    ]

    # Pass the expected functions
    assert_api_gateway_resources(
        pulumi_mocks,
        API_NAME,
        api_structure,
        [Funcs.FOLDER_HANDLER, Funcs.FOLDER2_HANDLER],
    )


@mark.parametrize(
    ("routes", "expected_api_structure", "expected_functions"),
    [
        # Case 1: Single handler in a file - no routing file
        (
            [
                ("GET", "/users", Funcs.USERS.handler),
                ("POST", "/users", Funcs.USERS.handler),
                ("PUT", "/users/{userId}", Funcs.USERS.handler),
            ],
            [
                Res(
                    PathPart.USERS,
                    [Method("GET", Funcs.USERS), Method("POST", Funcs.USERS)],
                    children=[Res(PathPart.USER_ID, [Method("PUT", Funcs.USERS)])],
                )
            ],
            [Funcs.USERS],
        ),
        # Case 3: Single handler in a folder - no routing file
        (
            [
                ("GET", "/folder/handler", Funcs.FOLDER_HANDLER.handler),
                ("POST", "/folder/handler", Funcs.FOLDER_HANDLER.handler),
            ],
            [
                Res(
                    "folder",
                    children=[
                        Res(
                            "handler",
                            [
                                Method("GET", Funcs.FOLDER_HANDLER),
                                Method("POST", Funcs.FOLDER_HANDLER),
                            ],
                        )
                    ],
                )
            ],
            [Funcs.FOLDER_HANDLER],
        ),
    ],
    ids=[
        "single_file_single_handler",
        "folder_single_handler",
    ],
)
def test_routing_file_generation(pulumi_mocks, routes, expected_api_structure, expected_functions):
    """Test that routing files are created only when needed and contain correct content."""
    # Arrange
    api = RestApi(API_NAME)

    # Add routes according to test case
    for verb, path, handler in routes:
        api.route(verb, path, handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    assert_api_gateway_resources(
        pulumi_mocks, API_NAME, expected_api_structure, expected_functions
    )


def test_empty_api(pulumi_mocks):
    """No routes: still the API, account, role, log group, deployment and stage."""
    # Arrange
    api = RestApi(API_NAME)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    assert_api_gateway_resources(pulumi_mocks, API_NAME, [], [])


def test_very_deep_paths(pulumi_mocks):
    """Test that API with deeply nested paths creates resources correctly."""
    # Arrange
    api = RestApi(API_NAME)

    # Create a deeply nested path with many segments
    deep_path = "/level1/level2/level3/level4/level5/level6/level7/level8/level9/level10"
    api.route("GET", deep_path, Funcs.SIMPLE.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    # Define expected API structure
    level10 = Res("level10", [Method("GET", Funcs.SIMPLE)])
    level9 = Res("level9", children=[level10])
    level8 = Res("level8", children=[level9])
    level7 = Res("level7", children=[level8])
    level6 = Res("level6", children=[level7])
    level5 = Res("level5", children=[level6])
    level4 = Res("level4", children=[level5])
    level3 = Res("level3", children=[level4])
    level2 = Res("level2", children=[level3])
    level1 = Res("level1", children=[level2])

    api_structure = [level1]

    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, [Funcs.SIMPLE])


def test_maximum_path_parameters(pulumi_mocks):
    """Test that API with maximum number of path parameters (10) creates resources correctly."""
    # Arrange
    api = RestApi(API_NAME)

    # Create a path with 10 parameters (maximum allowed)
    max_params_path = (
        "/{param1}/{param2}/{param3}/{param4}/{param5}/{param6}/{param7}/{param8}"
        "/{param9}/{param10}"
    )
    api.route("GET", max_params_path, Funcs.SIMPLE.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    # Define expected API structure
    param10 = Res("{param10}", [Method("GET", Funcs.SIMPLE)])
    param9 = Res("{param9}", children=[param10])
    param8 = Res("{param8}", children=[param9])
    param7 = Res("{param7}", children=[param8])
    param6 = Res("{param6}", children=[param7])
    param5 = Res("{param5}", children=[param6])
    param4 = Res("{param4}", children=[param5])
    param3 = Res("{param3}", children=[param4])
    param2 = Res("{param2}", children=[param3])
    param1 = Res("{param1}", children=[param2])

    api_structure = [param1]

    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, [Funcs.SIMPLE])


def test_multiple_apis_with_same_routes(pulumi_mocks):
    """Test that multiple APIs with identical routes can coexist without conflicts."""
    # Arrange - Create two APIs with identical route structures using existing handlers
    api1 = RestApi("user-api")
    api1.route("GET", "/users", Funcs.USERS.handler)
    api1.route("POST", "/users", Funcs.SIMPLE.handler)
    api1.route("GET", "/users/{id}", Funcs.ORDERS.handler)

    api2 = RestApi("admin-api")
    api2.route("GET", "/users", Funcs.SIMPLE2.handler)
    api2.route("POST", "/users", Funcs.FOLDER_PROCESS.handler)
    api2.route("GET", "/users/{id}", Funcs.FOLDER2_PROCESS.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return [api1.resources, api2.resources]

    deploy()

    # Assert
    def structure(get_users: Func, post_users: Func, get_user: Func) -> list[Res]:
        return [
            Res(
                PathPart.USERS,
                [Method("GET", get_users), Method("POST", post_users)],
                children=[Res("{id}", [Method("GET", get_user)])],
            )
        ]

    user_api_functions = [Funcs.USERS, Funcs.SIMPLE, Funcs.ORDERS]
    admin_api_functions = [Funcs.SIMPLE2, Funcs.FOLDER_PROCESS, Funcs.FOLDER2_PROCESS]
    assert_api_gateway_resources(
        pulumi_mocks, "user-api", structure(*user_api_functions), user_api_functions, seal=False
    )
    assert_api_gateway_resources(
        pulumi_mocks,
        "admin-api",
        structure(*admin_api_functions),
        admin_api_functions,
        seal=False,
    )
    pulumi_mocks.assert_res_counts(rest_api_counts(6, 4, 6, apis=2))


def test_function_instance_routed_from_two_apis_gets_a_permission_per_api(pulumi_mocks):
    """Permissions carry the API name, so the two don't collide on one Pulumi name."""
    fn = Function("shared-fn", handler=Funcs.USERS.handler)
    api1 = RestApi("user-api")
    api1.route("GET", "/users", fn)
    api2 = RestApi("admin-api")
    api2.route("GET", "/users", fn)

    @pulumi.runtime.test
    def deploy():
        return [api1.resources, api2.resources]

    deploy()

    for api_name in ("user-api", "admin-api"):
        pulumi_mocks.assert_res(
            f"{api_name}-permission-shared-fn",
            R.LAMBDA_PERMISSION,
            {
                "action": "lambda:InvokeFunction",
                "function": tn(TP + "shared-fn"),
                "principal": "apigateway.amazonaws.com",
                "sourceArn": API_INVOKE_SOURCE_ARN,
            },
        )
    pulumi_mocks.assert_res_counts(
        rest_api_counts(1, 2, 2, apis=2) + Counter({R.LAMBDA_PERMISSION: 1})
    )


def test_overlapping_route_patterns(pulumi_mocks):
    """Test that API with overlapping route patterns creates resources correctly."""
    # Arrange
    api = RestApi(API_NAME)

    # Create overlapping routes (parameter vs fixed path)
    api.route("GET", "/users/{userId}", Funcs.USERS.handler)
    api.route("GET", "/users/profile", Funcs.USERS.handler)
    api.route("GET", "/users/settings", Funcs.USERS.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    # Define expected API structure
    api_structure = [
        Res(
            PathPart.USERS,
            children=[
                Res(PathPart.USER_ID, [Method("GET", Funcs.USERS)]),
                Res("profile", [Method("GET", Funcs.USERS)]),
                Res("settings", [Method("GET", Funcs.USERS)]),
            ],
        )
    ]

    assert_api_gateway_resources(pulumi_mocks, API_NAME, api_structure, [Funcs.USERS])


def test_duplicate_routes_error():
    """Test that adding duplicate routes (same path and method) raises an error."""
    # Arrange
    api = RestApi(API_NAME)

    # Add the first route
    api.route("GET", "/users", Funcs.USERS.handler)

    # Add the same route again - this should raise an error
    with raises(ValueError, match="Route conflict"):
        api.route("GET", "/users", Funcs.SIMPLE.handler)


def test_conflicting_lambda_configurations():
    """Test that conflicting lambda configurations raises an error."""
    # Arrange
    api = RestApi(API_NAME)

    # Add routes with conflicting lambda configurations
    api.route("GET", "/users", Funcs.USERS.handler, memory=128)
    api.route("POST", "/users", Funcs.USERS.handler, memory=256)

    # This should raise an error when resources are created
    with raises(ValueError, match="Multiple routes try to configure the same Lambda function"):
        _ = api.resources


def test_api_with_edge_endpoint_and_custom_stage(pulumi_mocks):
    # Arrange
    config = RestApiConfig(endpoint_type="edge", stage_name="production")
    api = RestApi("test-api", config)
    api.route("GET", "/users", Funcs.SIMPLE.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    api_structure = [Res("users", [Method("GET", Funcs.SIMPLE)])]
    assert_api_gateway_resources(
        pulumi_mocks,
        "test-api",
        api_structure,
        [Funcs.SIMPLE],
        expected_endpoint_type="EDGE",
        expected_stage_name="production",
    )


def test_api_with_regional_endpoint_and_default_stage(pulumi_mocks):
    # Arrange
    config = RestApiConfig(endpoint_type="regional")
    api = RestApi("test-api", config)
    api.route("GET", "/orders", Funcs.ORDERS.handler)

    # Act
    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    # Assert
    api_structure = [Res("orders", [Method("GET", Funcs.ORDERS)])]
    assert_api_gateway_resources(
        pulumi_mocks,
        "test-api",
        api_structure,
        [Funcs.ORDERS],
        expected_endpoint_type="REGIONAL",
        expected_stage_name="v1",  # Default
    )


def test_rest_api_long_stage_name_is_hash_truncated(pulumi_mocks):
    """The Stage's Pulumi name is cut to the 128 limit (minus Pulumi's 8-char suffix) with
    a hash tail; the AWS stage name stays what the user set."""
    stage_name = "s" * 120
    api = RestApi("test-api", stage_name=stage_name)
    api.route("GET", "/users", Funcs.SIMPLE.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    (stage,) = pulumi_mocks.created(R.API_STAGE)
    assert_hash_truncated(stage.name, 120)
    assert stage.inputs["stageName"] == stage_name


def test_rest_api_long_permission_name_is_hash_truncated(pulumi_mocks):
    """The route permission's Pulumi name is cut to the 100-char statement-id limit (minus
    Pulumi's 8-char suffix) with a hash tail; it still points at the Function."""
    fn = Function("b" * 40, handler=Funcs.USERS.handler)
    api = RestApi("a" * 60)
    api.route("GET", "/users", fn)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    (permission,) = pulumi_mocks.created(R.LAMBDA_PERMISSION)
    assert_hash_truncated(permission.name, 92)
    assert permission.inputs["function"] == tn(TP + "b" * 40)


@pulumi.runtime.test
def test_route_rejects_after_resources_created(pulumi_mocks):
    api = RestApi("test-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="Cannot modify RestApi 'test-api' after resources"):
        api.route("POST", "/users", "functions/simple.handler")


@pulumi.runtime.test
def test_add_token_authorizer_rejects_after_resources_created(pulumi_mocks):
    api = RestApi("test-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="Cannot modify RestApi 'test-api' after resources"):
        api.add_token_authorizer("jwt", "functions/simple.handler")


@pulumi.runtime.test
def test_add_request_authorizer_rejects_after_resources_created(pulumi_mocks):
    api = RestApi("test-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="Cannot modify RestApi 'test-api' after resources"):
        api.add_request_authorizer("req-auth", "functions/simple.handler")


@pulumi.runtime.test
def test_add_cognito_authorizer_rejects_after_resources_created(pulumi_mocks):
    api = RestApi("test-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="Cannot modify RestApi 'test-api' after resources"):
        api.add_cognito_authorizer("cognito", user_pools=["arn:aws:cognito:us-east-1:123:pool"])


@pulumi.runtime.test
def test_default_auth_rejects_after_resources_created(pulumi_mocks):
    api = RestApi("test-api")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    with raises(RuntimeError, match="Cannot modify RestApi 'test-api' after resources"):
        api.default_auth = "IAM"


def test_rest_api_children_alias_their_old_names(pulumi_mocks, monkeypatch):
    """Deployed stacks keep their Resources, Methods and Integrations: the pre-rename names
    (braces dropped, `+` spelled `plus`, segments joined with '-', `root` for `/`) ride
    along as aliases."""
    old_names = spy_old_names(monkeypatch, RestApi)
    api = RestApi(API_NAME)
    api.route("GET", "/users/{id}/orders", handler=Funcs.SIMPLE.handler)
    api.route("POST", "/", handler=Funcs.SIMPLE.handler)
    api.route("GET", "/files/{proxy+}", handler=Funcs.SIMPLE.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert set(old_names) == {
        f"{TP}{API_NAME}-resource-users",
        f"{TP}{API_NAME}-resource-users-id",
        f"{TP}{API_NAME}-resource-users-id-orders",
        f"{TP}{API_NAME}-method-GET-users-id-orders",
        f"{TP}{API_NAME}-integration-GET-users-id-orders",
        f"{TP}{API_NAME}-method-POST-root",
        f"{TP}{API_NAME}-integration-POST-root",
        f"{TP}{API_NAME}-resource-files",
        f"{TP}{API_NAME}-resource-files-proxyplus",
        f"{TP}{API_NAME}-method-GET-files-proxyplus",
        f"{TP}{API_NAME}-integration-GET-files-proxyplus",
    }


def test_rest_api_routes_that_flattened_to_one_name_are_distinct(pulumi_mocks):
    """The old names dropped braces and joined segments with '-', so these pairs shared a
    name and the deploy died on a duplicate URN. The route itself is the name now."""
    api = RestApi(API_NAME)
    api.route("GET", "/user-profiles", handler=Funcs.SIMPLE.handler)
    api.route("GET", "/user/profiles", handler=Funcs.SIMPLE.handler)
    api.route("GET", "/users/{id}", handler=Funcs.SIMPLE.handler)
    api.route("GET", "/users/id", handler=Funcs.SIMPLE.handler)

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert {m.name for m in pulumi_mocks.created(R.API_METHOD)} == {
        f"{TP}{API_NAME}-method-GET /user-profiles",
        f"{TP}{API_NAME}-method-GET /user/profiles",
        f"{TP}{API_NAME}-method-GET /users/{{id}}",
        f"{TP}{API_NAME}-method-GET /users/id",
    }
