import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, cast

import pulumi
import pytest
from pulumi import StringAsset
from pulumi.runtime import set_mocks

from stelvio.aws.api_gateway import Api
from stelvio.aws.function import Function, FunctionAssetsRegistry, FunctionConfig
from stelvio.component import ComponentRegistry

from ..pulumi_mocks import (
    ACCOUNT_ID,
    DEFAULT_REGION,
    ROOT_RESOURCE_ID,
    SAMPLE_API_ID,
    PulumiTestMocks,
    tid,
    tn,
)
from .test_api_helper_functions import HANDLER_END, HANDLER_START

# Test prefix
TP = "test-test-"


# API resources (path parts)
class PathPart:
    USERS: str = "users"
    USER_ID: str = "{userId}"
    ORDERS: str = "orders"
    ORDER_ID: str = "{orderId}"
    ITEMS: str = "items"
    REPORT: str = "report"


# API configuration
class ApiConfig:
    NAME: str = "test-api"
    STAGE: str = "v1"


@dataclass
class Func:
    handler: str
    # folder: str
    name: str
    extra_assets: dict[str, StringAsset | Literal["SKIP"]] = field(default_factory=dict)
    timeout: int | None = None
    memory: int | None = None
    instance: Function | None = None


class Funcs:
    # Single-file functions
    SIMPLE: Func = Func("functions/simple.handler", "functions-simple")
    USERS: Func = Func("functions/users.handler", "functions-users")
    ORDERS: Func = Func("functions/orders.handler", "functions-orders")

    FOLDER_HANDLER: Func = Func("functions/folder::handler.fn", "functions-folder")
    FOLDER_HANDLER_FN2: Func = Func("functions/folder::handler.fn2", "functions-folder")
    FOLDER_HANDLER2: Func = Func("functions/folder::handler2.fn", "functions-folder")
    FOLDER2_HANDLER: Func = Func("functions/folder2::handler.fn", "functions-folder2")


@dataclass
class Method:
    verb: Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "ANY"]
    function: Func


@dataclass
class R:
    path_part: str
    methods: list[Method] = field(default_factory=list)
    children: list["R"] = field(default_factory=list)

    def full_path_parts(self, parent_parts: list[str]):
        return [*parent_parts, self.path_part]

    def name(self, parent_parts: list[str]):
        all_parts = self.full_path_parts(parent_parts)
        return f"resource-{'-'.join(all_parts)}".translate(str.maketrans("", "", "{}")).replace(
            "+", "plus"
        )


"""
Ok, so what we need to test?

In test_api_route_dataclass.py we test _ApiRoute to make sure it validates and
transforms inputs as needed.

In test_api_route.py we test route() method and _create_route() to make sure parameters
are properly checked and transformed and internal routes are created.

In tests_api_helper_functions.py we test internal functions to make sure that routes
are properly grouped by lambdas they use and that we generate proper routing file
content.

Here we need to test that that _create_resource creates all needed Pulumi resources to
deliver what user configured:

  1. [x] Rest api
  2. [x] Role for gateway and give with permission to write to cloudwatch
  3. [x] Account with the above role
  4. [x] For each route:
      a. [x] Resources
      b. [x] Method(s)
      c. [x] Lambda from handler (one per group)
      d. [x] Lambda resource policy so it can be called by given api gateway
      e. [x] Integration between method and lambda
  5. [x] Deployment
  6. [x] Stage
"""

LOG_GROUP_ARN_TEMPLATE = (
    f"arn:aws:logs:{DEFAULT_REGION}:{ACCOUNT_ID}:log-group:/aws/apigateway/{{name}}"
)
LAMBDA_INVOKE_ARN_TEMPLATE = (
    f"arn:aws:apigateway:{DEFAULT_REGION}:lambda:path/2015-03-31/"
    f"functions/arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:"
    f"function:{{function_name}}/invocations"
)
# Updated to match the broader permission source ARN used in the code
API_EXECUTION_ARN_TEMPLATE = (
    f"arn:aws:execute-api:{DEFAULT_REGION}:{ACCOUNT_ID}:{SAMPLE_API_ID}/*/*"
)

API_GATEWAY_ASSUME_ROLE_POLICY = [
    {
        "actions": ["sts:AssumeRole"],
        "principals": [{"identifiers": ["apigateway.amazonaws.com"], "type": "Service"}],
    }
]


def reset_api_gateway_caches():
    """Reset cached functions in the api_gateway module.

    This clears the function cache for specific cached functions
    that cause test isolation issues.
    """
    from stelvio.aws.api_gateway import (
        _create_api_gateway_account_and_role,
        _create_api_gateway_role,
    )

    # Clear the cache of these functions
    if hasattr(_create_api_gateway_role, "cache_clear"):
        _create_api_gateway_role.cache_clear()

    if hasattr(_create_api_gateway_account_and_role, "cache_clear"):
        _create_api_gateway_account_and_role.cache_clear()


@pytest.fixture
def pulumi_mocks():
    # Create a fresh mocks instance for each test
    # Reset API Gateway caches before each test to prevent cross-test contamination
    reset_api_gateway_caches()
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


# noinspection PyProtectedMember
@pytest.fixture
def component_registry():
    # Clear all registry data before each test
    ComponentRegistry._instances.clear()
    # ComponentRegistry._type_link_creators.clear()
    yield ComponentRegistry
    # Clear again after test completes
    ComponentRegistry._instances.clear()


def delete_files(directory: Path, filename: str):
    directory_path = directory
    for file_path in directory_path.rglob(filename):
        file_path.unlink()


@pytest.fixture(autouse=True)
def project_cwd(monkeypatch, pytestconfig):
    rootpath = pytestconfig.rootpath
    test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    monkeypatch.chdir(test_project_dir)
    yield test_project_dir
    delete_files(test_project_dir, "stlv_resources.py")


def assert_rest_api(mocks: PulumiTestMocks, name: str):
    rest_apis = mocks.created_rest_apis(TP + name)
    rest_api = rest_apis[0]
    assert rest_api.name == TP + name


def assert_deployment(mocks: PulumiTestMocks, api_name: str):
    deployments = mocks.created_deployments(f"{TP + api_name}-deployment")
    assert len(deployments) == 1
    deployment = deployments[0]
    # Verify deployment is linked to the expected REST API
    assert deployment.inputs["restApi"] == tid(TP + api_name)

    # Verify deployment has trigger mechanism for redeployment
    assert "triggers" in deployment.inputs
    # The triggers dictionary should have a 'configuration_hash' key with a hash value
    # This is used to trigger redeployment when the route configuration changes
    assert "configuration_hash" in deployment.inputs["triggers"]

    configuration_hash = deployment.inputs["triggers"]["configuration_hash"]
    assert isinstance(configuration_hash, str)
    # Check if the hash looks like a SHA256 hash (64 hex characters)
    assert len(configuration_hash) == 64
    assert all(c in "0123456789abcdef" for c in configuration_hash)


def assert_stage(mocks: PulumiTestMocks, api_name: str):
    stages = mocks.created_stages()
    assert len(stages) == 1
    stage = stages[0]

    # Verify stage is linked to the expected REST API and deployment
    assert stage.inputs["restApi"] == tid(TP + api_name)
    assert stage.inputs["deployment"] == tid(f"{TP + api_name}-deployment")
    assert stage.inputs["stageName"] == ApiConfig.STAGE

    # Verify expected access log settings are present
    expected_log_settings = {
        "destinationArn": LOG_GROUP_ARN_TEMPLATE.format(name=tn(TP + api_name)),
        "format": stage.inputs["accessLogSettings"]["format"],
    }

    assert stage.inputs["accessLogSettings"] == expected_log_settings

    assert stage.inputs["variables"] == {"loggingLevel": "INFO"}

    return stage


def assert_permissions(mocks: PulumiTestMocks, function: Func, api_name: str):
    print("PERMISSIONS")
    print("PERMISSIONS")
    print(mocks.created_permissions()[0].name)
    permission_name = (
        f"{TP}{api_name + '-' if not function.instance else ''}{function.name}-permission"
    )
    print("PERMI NAME")
    print(permission_name)
    permissions = mocks.created_permissions(permission_name)
    assert len(permissions) == 1
    for permission in permissions:
        # Verify lambda invoke permission
        assert permission.inputs["action"] == "lambda:InvokeFunction"
        # Verify principal is API Gateway
        assert permission.inputs["principal"] == "apigateway.amazonaws.com"
        # Verify function name if provided
        assert permission.inputs["function"] == tn(
            f"{TP}{api_name + '-' if not function.instance else ''}{function.name}"
        )
        # Verify source ARN is present and has correct format
        assert permission.inputs["sourceArn"] == API_EXECUTION_ARN_TEMPLATE

    return permissions


def assert_resources_methods_and_integrations(
    mocks: PulumiTestMocks,
    api_name: str,
    api_structure: list[R],
    parent_parts: list[str] | None = None,
    parent_id: str = ROOT_RESOURCE_ID,
):
    if not parent_parts:
        parent_parts = []

    api_id = tid(TP + api_name)

    for resource in api_structure:
        matching_resources = [
            r
            for r in mocks.created_api_resources()
            if r.inputs["pathPart"] == resource.path_part
            and r.inputs["parentId"] == parent_id
            and r.inputs["restApi"] == api_id
        ]
        assert len(matching_resources) == 1
        expected_name = TP + resource.name(parent_parts)
        assert matching_resources[0].name == expected_name

        resource_id = tid(expected_name)

        # Find methods for this resource
        resource_methods = [
            m
            for m in mocks.created_methods()
            if m.inputs["resourceId"] == resource_id and m.inputs["restApi"] == api_id
        ]

        # Verify found methods match expected
        method_names = [m.inputs["httpMethod"] for m in resource_methods]
        assert sorted(method_names) == sorted([m.verb for m in resource.methods])

        # Find integrations for this resource
        resource_integrations = {
            i
            for i in mocks.created_integrations()
            if i.inputs["resourceId"] == resource_id and i.inputs["restApi"] == api_id
        }
        # Make sure we have as many integrations as expected methods
        assert len(resource_integrations) == len(resource_methods)

        method_integration_map = {i.inputs["httpMethod"]: i for i in resource_integrations}

        # Verify found integrations match methods
        assert sorted(method_integration_map.keys()) == sorted([m.verb for m in resource.methods])

        for method in resource.methods:
            integration = method_integration_map[method.verb]
            assert integration.inputs["type"] == "AWS_PROXY"
            assert integration.inputs["integrationHttpMethod"] == "POST"
            assert integration.inputs["httpMethod"] == method.verb
            # Check function ARN in URI
            expected_uri = LAMBDA_INVOKE_ARN_TEMPLATE.format(
                function_name=tn(
                    f"{TP}"
                    f"{api_name + '-' if not method.function.instance else ''}"
                    f"{method.function.name}"
                )
            )
            assert integration.inputs["uri"] == expected_uri

        # Process children recursively
        assert_resources_methods_and_integrations(
            mocks, api_name, resource.children, resource.full_path_parts(parent_parts), resource_id
        )


def assert_api_gateway_resources(
    mocks: PulumiTestMocks, api_name: str, api_structure: list[R], expected_functions: list[Func]
):
    assert_rest_api(mocks, api_name)
    assert_api_account_and_role(mocks)

    # Check functions
    assert_stelvio_functions(expected_functions, api_name)

    # Verify methods and integrations for all resources
    assert_resources_methods_and_integrations(mocks, api_name, api_structure)

    # Check deployment and stage
    assert_deployment(mocks, api_name)
    assert_stage(mocks, api_name)

    for function in expected_functions:
        assert_permissions(mocks, function, api_name)


def assert_api_account_and_role(mocks: PulumiTestMocks):
    # Check Role
    roles = mocks.created_roles("api-gateway-role")
    assert len(roles) == 1
    role = roles[0]
    assert role.inputs == {"assumeRolePolicy": json.dumps(API_GATEWAY_ASSUME_ROLE_POLICY)}

    # Check Role attachment
    role_attachments = mocks.created_role_policy_attachments(
        "api-gateway-role-logs-policy-attachment"
    )
    assert len(role_attachments) == 1
    logs_role_attachment = role_attachments[0]
    assert logs_role_attachment.name == "api-gateway-role-logs-policy-attachment"
    assert (
        logs_role_attachment.inputs["policyArn"]
        == "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
    )
    assert logs_role_attachment.inputs["role"] == "api-gateway-role-test-name"

    # Check Account
    accounts = mocks.created_api_accounts("api-gateway-account")
    assert len(accounts) == 1
    account = accounts[0]
    assert (
        account.inputs["cloudwatchRoleArn"]
        == f"arn:aws:iam::{ACCOUNT_ID}:role/api-gateway-role-test-name"
    )


def assert_stelvio_functions(
    expected_functions: list[Func], api_name: str, allow_extra: bool = False
):
    # Get all Function instances from the registry
    # noinspection PyProtectedMember
    functions = ComponentRegistry._instances.get(Function, [])

    # Build a map of functions by name for easier lookup
    function_map = {f.name: f for f in functions}

    # Check each expected function
    for expected_fn in expected_functions:
        expected_fn_name = (
            f"{api_name}-{expected_fn.name}" if not expected_fn.instance else expected_fn.name
        )
        assert expected_fn_name in function_map
        created_fn = cast("Function", function_map[expected_fn_name])
        # noinspection PyUnresolvedReferences
        assert created_fn.config.handler == expected_fn.handler

        if expected_fn.memory or created_fn.config.memory:
            assert created_fn.config.memory == expected_fn.memory
        if expected_fn.timeout or created_fn.config.timeout:
            assert created_fn.config.timeout == expected_fn.timeout
        if expected_fn.instance:
            assert created_fn == expected_fn.instance

        assets_map = FunctionAssetsRegistry.get_assets_map(created_fn)
        assert assets_map.keys() == expected_fn.extra_assets.keys()
        for name, expected_asset in expected_fn.extra_assets.items():
            if isinstance(expected_asset, str) and expected_asset == "SKIP":
                continue
            asset = assets_map[name]
            assert isinstance(asset, StringAsset)
            assert asset.text == expected_asset.text

    # Check that there are no unexpected functions
    if not allow_extra:
        unexpected = set(function_map.keys()) - {
            f"{api_name}-{f.name}" if not f.instance else f.name for f in expected_functions
        }
        assert not unexpected


@pulumi.runtime.test
def test_api_properties(pulumi_mocks):
    """Test that Api.resources property correctly provides access to created resources."""
    api = Api("test-api")
    api.route("GET", "/users", "functions/simple.handler")

    # Create the resource
    _ = api.resources

    def check_resources(args):
        rest_api_id, stage_id, deployment_id, api_arn, invoke_url = args

        # Verify resource IDs match expected patterns
        assert rest_api_id == TP + "test-api-test-id"
        assert stage_id == TP + "test-api-v1-test-id"
        assert deployment_id == TP + "test-api-deployment-test-id"

        # Check that convenience properties have expected formats
        assert api_arn == "arn:aws:apigateway:us-east-1::/restapis/12345abcde"
        expected_url = f"https://{TP}test-api-test-id.execute-api.us-east-1.amazonaws.com/v1"
        assert invoke_url == expected_url

    pulumi.Output.all(
        api.resources.rest_api.id,
        api.resources.stage.id,
        api.resources.deployment.id,
        api.api_arn,
        api.invoke_url,
    ).apply(check_resources)


@pulumi.runtime.test
def test_rest_api_basic(pulumi_mocks, component_registry):
    """1. Basic API Resource Creation:
    - Single route with GET method and simple handler
    """
    # Arrange
    api = Api(ApiConfig.NAME)
    api.route("GET", f"/{PathPart.USERS}", Funcs.SIMPLE.handler)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        api_structure = [R(PathPart.USERS, [Method("GET", Funcs.SIMPLE)])]
        assert_api_gateway_resources(pulumi_mocks, ApiConfig.NAME, api_structure, [Funcs.SIMPLE])

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_api_resources_multiple_paths(pulumi_mocks, component_registry):
    """2. Multiple Routes:
    - Routes with multiple paths ("/users", "/users/{id}", "/orders")
    - Verify correct resource creation for each path
    - Verify parent-child relationships between resources
    """
    # Arrange - create an API with multiple simple routes
    api = Api(ApiConfig.NAME)
    api.route("GET", f"/{PathPart.USERS}", Funcs.USERS.handler)
    # Add a simple parameter route
    api.route("GET", f"/{PathPart.USERS}/{PathPart.USER_ID}", Funcs.USERS.handler)

    api.route("GET", f"/{PathPart.ORDERS}", Funcs.ORDERS.handler)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        # Define API structure using the Res dataclass
        api_structure = [
            R(
                PathPart.USERS,
                [Method("GET", Funcs.USERS)],
                children=[R(PathPart.USER_ID, [Method("GET", Funcs.USERS)])],
            ),
            R(PathPart.ORDERS, [Method("GET", Funcs.ORDERS)]),
        ]
        assert_api_gateway_resources(
            pulumi_mocks, ApiConfig.NAME, api_structure, [Funcs.USERS, Funcs.ORDERS]
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_api_path_parameter_handling(pulumi_mocks, component_registry):
    """3. Path Parameter Handling:
    - Simple parameters ("/users/{id}")
    - Deep nested parameters ("/users/{userId}/orders/{orderId}")
    - Greedy parameters ("/files/{proxy+}")
    - Multi-segment paths ("/api/v1/resources")
    """
    # Arrange
    api = Api(ApiConfig.NAME)
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
    _ = api.resources

    # Assert
    # Define API structure using the Res dataclass
    api_structure = [
        R(  # /users/{userId}
            PathPart.USERS,
            children=[
                R(
                    PathPart.USER_ID,
                    [Method("GET", Funcs.USERS)],
                    children=[
                        # /users/{userId}/orders/{orderId}
                        R(
                            PathPart.ORDERS,
                            children=[R(PathPart.ORDER_ID, [Method("GET", Funcs.ORDERS)])],
                        )
                    ],
                )
            ],
        ),
        # /files/{proxy+}
        R("files", children=[R("{proxy+}", [Method("GET", Funcs.SIMPLE)])]),
        R(  # /api/v1/resources
            "api", children=[R("v1", [], children=[R("resources", [Method("GET", Funcs.SIMPLE)])])]
        ),
    ]

    def check_resources(_):
        assert_api_gateway_resources(
            pulumi_mocks, ApiConfig.NAME, api_structure, [Funcs.USERS, Funcs.ORDERS, Funcs.SIMPLE]
        )

    api.resources.stage.id.apply(check_resources)


@pytest.mark.parametrize(
    ("route_style", "include_any_method"),
    [
        ("individual", False),  # Individual calls to api.route for each HTTP method
        ("list", False),  # Single call with list of HTTP methods
        ("individual", True),  # With ANY method on second path
        ("list", True),  # With ANY method on second path
    ],
    ids=["individual", "list", "individual_with_any", "list_with_any"],
)
@pulumi.runtime.test
def test_http_method_handling(pulumi_mocks, component_registry, route_style, include_any_method):
    """4. HTTP Method Handling:
    - Multiple methods on single route (GET, POST, PUT, DELETE on "/users")
    - ANY method for catch-all endpoint ("/reports")
    - Verify method resources and integrations for each HTTP method
    - Test both individual method routing and list-based method routing
    """
    # Arrange
    api = Api(ApiConfig.NAME)

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
    _ = api.resources

    # Assert
    def check_resources(_):
        # Define expected API structure
        api_structure = [
            R(
                PathPart.USERS,
                [
                    Method("GET", Funcs.USERS),
                    Method("POST", Funcs.USERS),
                    Method("PUT", Funcs.USERS),
                    Method("DELETE", Funcs.USERS),
                ],
            ),
            R(PathPart.ORDERS, [Method("GET", Funcs.ORDERS)]),
        ]
        expected_functions = [Funcs.USERS, Funcs.ORDERS]

        # Add report resource with ANY method if required
        if include_any_method:
            api_structure.append(R(PathPart.REPORT, [Method("ANY", Funcs.SIMPLE)]))
            expected_functions.append(Funcs.SIMPLE)

        assert_api_gateway_resources(
            pulumi_mocks, ApiConfig.NAME, api_structure, expected_functions
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_function_instance_handler_configuration(pulumi_mocks, component_registry):
    """5. Handler Configuration Types - Function Object:
       - Test Function object passed to route

    Verifies that when a Function object is provided to api.route(),
    the existing function is reused rather than creating a new one.
    """
    # Arrange
    api = Api(ApiConfig.NAME)

    custom_function = Function(
        "test-custom-function", FunctionConfig(handler=Funcs.USERS.handler, memory=256, timeout=60)
    )

    # Configure route with Function object
    api.route("GET", f"/{PathPart.USERS}", custom_function)

    # Act
    _ = api.resources

    def check_resources(_):
        expected_function = Func(
            custom_function.config.handler,
            custom_function.name,
            timeout=custom_function.config.timeout,
            memory=custom_function.config.memory,
            instance=custom_function,
        )
        api_structure = [R(PathPart.USERS, [Method("GET", expected_function)])]
        assert_api_gateway_resources(
            pulumi_mocks, ApiConfig.NAME, api_structure, [expected_function]
        )

    api.resources.stage.id.apply(check_resources)


@pytest.mark.parametrize(
    ("args", "kwargs"),
    [
        ([], {"handler": Funcs.USERS.handler, "memory": 256, "timeout": 60}),
        ([FunctionConfig(handler=Funcs.USERS.handler, memory=256, timeout=60)], {}),
        ([{"handler": Funcs.USERS.handler, "memory": 256, "timeout": 60}], {}),
    ],
    ids=["string_handler_and_opts", "function_config_handler", "function_config_dict_handler"],
)
@pulumi.runtime.test
def test_route_handler_configuration__(pulumi_mocks, component_registry, args, kwargs):
    # Arrange
    api = Api(ApiConfig.NAME)
    api.route("GET", f"/{PathPart.USERS}", *args, **kwargs)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        expected_fn = replace(Funcs.USERS, timeout=60, memory=256)
        api_structure = [R(PathPart.USERS, [Method("GET", expected_fn)])]
        assert_api_gateway_resources(pulumi_mocks, ApiConfig.NAME, api_structure, [expected_fn])

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_function_reuse_single_file(pulumi_mocks, component_registry):
    # Arrange
    api = Api(ApiConfig.NAME)

    # Configure multiple routes pointing to the same single-file function
    api.route("GET", f"/{PathPart.USERS}", Funcs.USERS.handler)
    api.route("POST", f"/{PathPart.USERS}", Funcs.USERS.handler)  # Same handler
    # Same handler with different path
    api.route("PUT", f"/{PathPart.USERS}/{PathPart.USER_ID}", Funcs.USERS.handler)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        api_structure = [
            R(
                PathPart.USERS,
                [Method("GET", Funcs.USERS), Method("POST", Funcs.USERS)],
                children=[R(PathPart.USER_ID, [Method("PUT", Funcs.USERS)])],
            )
        ]
        assert_api_gateway_resources(pulumi_mocks, ApiConfig.NAME, api_structure, [Funcs.USERS])

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_function_separate_single_files(pulumi_mocks, component_registry):
    # Arrange
    api = Api(ApiConfig.NAME)

    # Configure routes pointing to different single-file functions
    api.route("GET", f"/{PathPart.USERS}", Funcs.USERS.handler)
    api.route("GET", f"/{PathPart.ORDERS}", Funcs.ORDERS.handler)
    api.route("GET", f"/{PathPart.ITEMS}", Funcs.SIMPLE.handler)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        api_structure = [
            R(PathPart.USERS, [Method("GET", Funcs.USERS)]),
            R(PathPart.ORDERS, [Method("GET", Funcs.ORDERS)]),
            R("items", [Method("GET", Funcs.SIMPLE)]),
        ]

        assert_api_gateway_resources(
            pulumi_mocks, ApiConfig.NAME, api_structure, [Funcs.USERS, Funcs.ORDERS, Funcs.SIMPLE]
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_function_reuse_folder_based(pulumi_mocks, component_registry):
    # Arrange
    api = Api(ApiConfig.NAME)

    # Configure routes pointing to different handlers but in the same folder function
    api.route("GET", "/folder/handler", Funcs.FOLDER_HANDLER.handler)
    api.route("POST", "/folder/handler2", Funcs.FOLDER_HANDLER2.handler)
    api.route("PUT", "/folder/handler2", Funcs.FOLDER_HANDLER2.handler)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        api_structure = [
            R(
                "folder",
                children=[
                    R("handler", [Method("GET", Funcs.FOLDER_HANDLER)]),
                    R(
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
            ApiConfig.NAME,
            api_structure,
            [replace(Funcs.FOLDER_HANDLER, extra_assets={"stlv_routing_handler.py": "SKIP"})],
        )

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_lambda_function_separate_folder_based(pulumi_mocks, component_registry):
    # Arrange
    api = Api(ApiConfig.NAME)

    # Configure routes pointing to different folder-based functions
    api.route("GET", "/folder/handler", Funcs.FOLDER_HANDLER.handler)
    api.route("GET", "/folder2/handler", Funcs.FOLDER2_HANDLER.handler)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        api_structure = [
            R("folder", children=[R("handler", [Method("GET", Funcs.FOLDER_HANDLER)])]),
            R("folder2", children=[R("handler", [Method("GET", Funcs.FOLDER2_HANDLER)])]),
        ]

        # Pass the expected functions
        assert_api_gateway_resources(
            pulumi_mocks,
            ApiConfig.NAME,
            api_structure,
            [Funcs.FOLDER_HANDLER, Funcs.FOLDER2_HANDLER],
        )

    api.resources.stage.id.apply(check_resources)


@pytest.mark.parametrize(
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
                R(
                    PathPart.USERS,
                    [Method("GET", Funcs.USERS), Method("POST", Funcs.USERS)],
                    children=[R(PathPart.USER_ID, [Method("PUT", Funcs.USERS)])],
                )
            ],
            [Funcs.USERS],
        ),
        #     # Case 2: Multiple handlers in a file - routing file needed
        (
            [
                ("GET", "/users", "functions/users.get_users"),
                ("POST", "/users", "functions/users.create_user"),
            ],
            [R(PathPart.USERS, [Method("GET", Funcs.USERS), Method("POST", Funcs.USERS)])],
            [
                replace(
                    Funcs.USERS,
                    handler="functions/users.get_users",
                    extra_assets={
                        "stlv_routing_handler.py": StringAsset(
                            "\n".join(
                                [
                                    *HANDLER_START,
                                    "from users import get_users, create_user",
                                    "\n\nROUTES = {",
                                    '    "GET /users": get_users,',
                                    '    "POST /users": create_user,',
                                    "}",
                                    *HANDLER_END,
                                ]
                            )
                        )
                    },
                )
            ],
        ),
        # Case 3: Single handler in a folder - no routing file
        (
            [
                ("GET", "/folder/handler", Funcs.FOLDER_HANDLER.handler),
                ("POST", "/folder/handler", Funcs.FOLDER_HANDLER.handler),
            ],
            [
                R(
                    "folder",
                    children=[
                        R(
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
        # Case 4: Multiple handlers in a folder (different files) - routing file needed
        (
            [
                ("GET", "/folder/handler", Funcs.FOLDER_HANDLER.handler),
                ("POST", "/folder/handler2", Funcs.FOLDER_HANDLER2.handler),
            ],
            [
                R(
                    "folder",
                    children=[
                        R("handler", [Method("GET", Funcs.FOLDER_HANDLER)]),
                        R("handler2", [Method("POST", Funcs.FOLDER_HANDLER2)]),
                    ],
                )
            ],
            [
                replace(
                    Funcs.FOLDER_HANDLER,
                    extra_assets={
                        "stlv_routing_handler.py": StringAsset(
                            "\n".join(
                                [
                                    *HANDLER_START,
                                    "from handler import fn",
                                    "from handler2 import fn as fn_handler2",
                                    "\n\nROUTES = {",
                                    '    "GET /folder/handler": fn,',
                                    '    "POST /folder/handler2": fn_handler2,',
                                    "}",
                                    *HANDLER_END,
                                ]
                            )
                        )
                    },
                )
            ],
        ),
        # Case 5: Multiple handlers in a folder (same file) - routing file needed
        (
            [
                ("GET", "/folder/handler", Funcs.FOLDER_HANDLER.handler),
                ("POST", "/folder/handler2", Funcs.FOLDER_HANDLER_FN2.handler),
            ],
            [
                R(
                    "folder",
                    children=[
                        R("handler", [Method("GET", Funcs.FOLDER_HANDLER)]),
                        R("handler2", [Method("POST", Funcs.FOLDER_HANDLER_FN2)]),
                    ],
                )
            ],
            [
                replace(
                    Funcs.FOLDER_HANDLER,
                    extra_assets={
                        "stlv_routing_handler.py": StringAsset(
                            "\n".join(
                                [
                                    *HANDLER_START,
                                    "from handler import fn, fn2",
                                    "\n\nROUTES = {",
                                    '    "GET /folder/handler": fn,',
                                    '    "POST /folder/handler2": fn2,',
                                    "}",
                                    *HANDLER_END,
                                ]
                            )
                        )
                    },
                )
            ],
        ),
    ],
    ids=[
        "single_file_single_handler",
        "single_file_multiple_handlers",
        "folder_single_handler",
        "folder_multiple_handlers",
        "folder_multiple_handlers2",
    ],
)
@pulumi.runtime.test
def test_routing_file_generation(
    pulumi_mocks, component_registry, routes, expected_api_structure, expected_functions
):
    """Test that routing files are created only when needed and contain correct content."""
    # Arrange
    api = Api(ApiConfig.NAME)

    # Add routes according to test case
    for verb, path, handler in routes:
        api.route(verb, path, handler)

    # Act
    _ = api.resources

    # Assert
    def check_routing_file(_):
        assert_api_gateway_resources(
            pulumi_mocks, ApiConfig.NAME, expected_api_structure, expected_functions
        )

    api.resources.stage.id.apply(check_routing_file)


@pulumi.runtime.test
def test_empty_api(pulumi_mocks, component_registry):
    """Test that an API with no routes creates the basic resources correctly."""
    # Arrange
    api = Api(ApiConfig.NAME)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        # Verify base API Gateway resources with empty API structure and functions
        assert_api_gateway_resources(pulumi_mocks, ApiConfig.NAME, [], [])

        # Additional checks to verify no resources were created
        assert len(pulumi_mocks.created_api_resources()) == 0
        assert len(pulumi_mocks.created_methods()) == 0
        assert len(pulumi_mocks.created_integrations()) == 0

        # Verify no Lambda functions were created
        # noinspection PyProtectedMember
        functions = ComponentRegistry._instances.get(Function, [])
        assert len(functions) == 0

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_very_deep_paths(pulumi_mocks, component_registry):
    """Test that API with deeply nested paths creates resources correctly."""
    # Arrange
    api = Api(ApiConfig.NAME)

    # Create a deeply nested path with many segments
    deep_path = "/level1/level2/level3/level4/level5/level6/level7/level8/level9/level10"
    api.route("GET", deep_path, Funcs.SIMPLE.handler)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        # Define expected API structure
        level10 = R("level10", [Method("GET", Funcs.SIMPLE)])
        level9 = R("level9", children=[level10])
        level8 = R("level8", children=[level9])
        level7 = R("level7", children=[level8])
        level6 = R("level6", children=[level7])
        level5 = R("level5", children=[level6])
        level4 = R("level4", children=[level5])
        level3 = R("level3", children=[level4])
        level2 = R("level2", children=[level3])
        level1 = R("level1", children=[level2])

        api_structure = [level1]

        assert_api_gateway_resources(pulumi_mocks, ApiConfig.NAME, api_structure, [Funcs.SIMPLE])

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_maximum_path_parameters(pulumi_mocks, component_registry):
    """Test that API with maximum number of path parameters (10) creates resources correctly."""
    # Arrange
    api = Api(ApiConfig.NAME)

    # Create a path with 10 parameters (maximum allowed)
    max_params_path = (
        "/{param1}/{param2}/{param3}/{param4}/{param5}/{param6}/{param7}/{param8}"
        "/{param9}/{param10}"
    )
    api.route("GET", max_params_path, Funcs.SIMPLE.handler)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        # Define expected API structure
        param10 = R("{param10}", [Method("GET", Funcs.SIMPLE)])
        param9 = R("{param9}", children=[param10])
        param8 = R("{param8}", children=[param9])
        param7 = R("{param7}", children=[param8])
        param6 = R("{param6}", children=[param7])
        param5 = R("{param5}", children=[param6])
        param4 = R("{param4}", children=[param5])
        param3 = R("{param3}", children=[param4])
        param2 = R("{param2}", children=[param3])
        param1 = R("{param1}", children=[param2])

        api_structure = [param1]

        assert_api_gateway_resources(pulumi_mocks, ApiConfig.NAME, api_structure, [Funcs.SIMPLE])

    api.resources.stage.id.apply(check_resources)


@pulumi.runtime.test
def test_overlapping_route_patterns(pulumi_mocks, component_registry):
    """Test that API with overlapping route patterns creates resources correctly."""
    # Arrange
    api = Api(ApiConfig.NAME)

    # Create overlapping routes (parameter vs fixed path)
    api.route("GET", "/users/{userId}", Funcs.USERS.handler)
    api.route("GET", "/users/profile", Funcs.USERS.handler)
    api.route("GET", "/users/settings", Funcs.USERS.handler)

    # Act
    _ = api.resources

    # Assert
    def check_resources(_):
        # Define expected API structure
        api_structure = [
            R(
                PathPart.USERS,
                children=[
                    R(PathPart.USER_ID, [Method("GET", Funcs.USERS)]),
                    R("profile", [Method("GET", Funcs.USERS)]),
                    R("settings", [Method("GET", Funcs.USERS)]),
                ],
            )
        ]

        assert_api_gateway_resources(pulumi_mocks, ApiConfig.NAME, api_structure, [Funcs.USERS])

    api.resources.stage.id.apply(check_resources)


def test_duplicate_routes_error():
    """Test that adding duplicate routes (same path and method) raises an error."""
    # Arrange
    api = Api(ApiConfig.NAME)

    # Add the first route
    api.route("GET", "/users", Funcs.USERS.handler)

    # Add the same route again - this should raise an error
    with pytest.raises(ValueError, match="Route conflict"):
        api.route("GET", "/users", Funcs.SIMPLE.handler)


def test_conflicting_lambda_configurations():
    """Test that conflicting lambda configurations raises an error."""
    # Arrange
    api = Api(ApiConfig.NAME)

    # Add routes with conflicting lambda configurations
    api.route("GET", "/users", Funcs.USERS.handler, memory=128)
    api.route("POST", "/users", Funcs.USERS.handler, memory=256)

    # This should raise an error when resources are created
    with pytest.raises(
        ValueError, match="Multiple routes trying to configure the same lambda function"
    ):
        _ = api.resources
