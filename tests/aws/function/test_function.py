"""
Stelvio does lot of things and we need to test them all but not all at once.
Also at some point we want to test infra with Pulumi's property testing and even with
integration testing. But we need to start with making sure that Stelvio creates proper
Pulumi resources.

For Function we need to test:

- [x] Validation of FunctionConfig
- [x] FunctionConfig helper properties
- [x] Validation of Function's init (_parse_config)
- [x] Check that FunctionConfigDict matches FunctionConfig
- [x] Check that Function properties return proper value
- [x] Check that `_create_resource`:
        - [x] Creates proper lambda role with proper iam statements
        - [x] Creates proper resource file for lambda
        - [x] Creates proper resource file for IDE (test with multiple single-file
                lambdas in the same folder)
        - [x] Creates properly configured Pulumi's Function object:
                - [x] that have proper envvars
                - [x] Uses routing handler from FunctionAssetsRegistry if present
                - [x] Uses settings passed from config: handler, memory, timeout
                - [x] With packaged proper code for single file and folder based lambdas
"""

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Tuple, Type

import pulumi
import pytest
from pulumi import AssetArchive, FileAsset, StringAsset, RemoteAsset, Asset
from pulumi.runtime import set_mocks, MockResourceArgs

from stelvio.aws.function import (
    Function,
    DEFAULT_MEMORY,
    DEFAULT_RUNTIME,
    DEFAULT_TIMEOUT,
    FunctionAssetsRegistry,
    LinkPropertiesRegistry,
)
from stelvio.aws.permission import AwsPermission
from stelvio.link import Linkable, Link
from tests.aws.pulumi_mocks import PulumiTestMocks

LAMBDA_ASSUME_ROLE_POLICY = [
    {
        "actions": ["sts:AssumeRole"],
        "principals": [{"identifiers": ["lambda.amazonaws.com"], "type": "Service"}],
    }
]

LINK_FILE_IMPORTS = """import os
from dataclasses import dataclass
from typing import Final
from functools import cached_property"""

TEST_LINK_DATACLASS_TXT = """@dataclass(frozen=True)
class TestLinkResource:
    @cached_property
    def name(self) -> str:
        return os.getenv("STLV_TEST_LINK_NAME")

    @cached_property
    def timeout(self) -> str:
        return os.getenv("STLV_TEST_LINK_TIMEOUT")
"""

TEST_LINK_2_DATACLASS_TXT = """@dataclass(frozen=True)
class TestLink2Resource:
    @cached_property
    def name2(self) -> str:
        return os.getenv("STLV_TEST_LINK2_NAME2")

    @cached_property
    def timeout2(self) -> str:
        return os.getenv("STLV_TEST_LINK2_TIMEOUT2")
"""

TEST_LINK_FILE_CONTENT = f"""{LINK_FILE_IMPORTS}
\n\n{TEST_LINK_DATACLASS_TXT}\n
@dataclass(frozen=True)
class LinkedResources:
    testLink: Final[TestLinkResource] = TestLinkResource()
\n
Resources: Final = LinkedResources()"""

TEST_LINK_FILE_CONTENT_IDE = TEST_LINK_FILE_CONTENT

TEST_LINK_2_FILE_CONTENT = f"""{LINK_FILE_IMPORTS}
\n\n{TEST_LINK_2_DATACLASS_TXT}\n
@dataclass(frozen=True)
class LinkedResources:
    testLink2: Final[TestLink2Resource] = TestLink2Resource()
\n
Resources: Final = LinkedResources()"""

# In case of single-file functions all functions in the same folder share one
# stlv_resources.py file for IDE which contains all resources from all single-file
# lambdas within a folder
TEST_LINK_2_FILE_CONTENT_IDE_SF = f"""{LINK_FILE_IMPORTS}
\n\n{TEST_LINK_DATACLASS_TXT}\n\n{TEST_LINK_2_DATACLASS_TXT}\n
@dataclass(frozen=True)
class LinkedResources:
    testLink: Final[TestLinkResource] = TestLinkResource()
    testLink2: Final[TestLink2Resource] = TestLink2Resource()
\n
Resources: Final = LinkedResources()"""

# In case of folder-based functions each function has its own folder (unless two
# instances use same folder)  and so ih has its own  stlv_resources.py file for IDE
# which is same as the one packaged with lambda
TEST_LINK_2_FILE_CONTENT_IDE_FB = TEST_LINK_2_FILE_CONTENT


@pytest.fixture(autouse=True)
def clean_registries():
    yield
    LinkPropertiesRegistry._folder_links_properties_map = {}


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    yield mocks


def delete_files(directory, filename):
    for file_path in Path(directory).rglob(filename):
        file_path.unlink()


@pytest.fixture(autouse=True)
def project_cwd(monkeypatch, pytestconfig):
    rootpath = pytestconfig.rootpath
    test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    print(f"Data directory: {test_project_dir}")
    monkeypatch.chdir(test_project_dir)
    yield test_project_dir
    delete_files(test_project_dir, "stlv_resources.py")


def assert_function_configuration(function_args, name, handler, envars):
    # Check lambda configuration
    assert function_args.name == name
    assert function_args.inputs["handler"] == handler
    assert function_args.inputs["runtime"] == DEFAULT_RUNTIME
    assert function_args.inputs["memorySize"] == DEFAULT_MEMORY
    assert function_args.inputs["timeout"] == DEFAULT_TIMEOUT
    assert function_args.inputs["environment"] == {"variables": envars}


def assert_function_code(
    function_args: MockResourceArgs,
    project_cwd,
    assets: dict[str, Tuple[Type[Asset], str]],
):
    # Check lambda code
    code: AssetArchive = function_args.inputs["code"]

    for name, (asset_type, path_text_uri) in assets.items():
        asset = code.assets[name]
        assert isinstance(asset, asset_type)
        if isinstance(asset, FileAsset):
            assert asset.path == str(project_cwd / path_text_uri)
        elif isinstance(asset, StringAsset):
            assert asset.text == path_text_uri
        elif isinstance(asset, RemoteAsset):
            assert asset.uri == path_text_uri


@dataclass
class FunctionTestCase:
    test_id: str
    name: str
    input_handler: str
    expected_handler: str
    expected_code_assets: dict[str, Tuple[Type[Asset], str]]
    extra_assets_map: dict[str, Asset] | None = None

    links: list[Link | Linkable] = field(default_factory=list)
    expected_envars: dict[str, str | int] = field(default_factory=dict)
    expected_policy: list[dict[str, list[str]]] = None
    expected_ide_file: Tuple[str, str] | None = None


SIMPLE_SF_TC = FunctionTestCase(
    test_id="simple_single_file",
    name="simple-single-file-function",
    input_handler="functions/simple.handler",
    expected_handler="simple.handler",
    expected_code_assets={"simple.py": (FileAsset, "functions/simple.py")},
)
SIMPLE_FB_TC = FunctionTestCase(
    test_id="simple_folder_based",
    name="simple-folder-based-function",
    input_handler="functions/folder::handler.process",
    expected_handler="handler.process",
    expected_code_assets={"handler.py": (FileAsset, "functions/folder/handler.py")},
)
ROUTING_SF_TC = replace(
    SIMPLE_SF_TC,
    test_id="routing_handler_single_file",
    extra_assets_map={"stlv_routing_handler.py": StringAsset("routing-handler")},
    expected_handler="stlv_routing_handler.lambda_handler",
    expected_code_assets={
        "simple.py": (FileAsset, "functions/simple.py"),
        "stlv_routing_handler.py": (StringAsset, "routing-handler"),
    },
)
ROUTING_FB_TC = replace(
    SIMPLE_FB_TC,
    test_id="routing_handler_folder_based",
    extra_assets_map=ROUTING_SF_TC.extra_assets_map,
    expected_handler=ROUTING_SF_TC.expected_handler,
    expected_code_assets={
        "handler.py": (FileAsset, "functions/folder/handler.py"),
        "stlv_routing_handler.py": (StringAsset, "routing-handler"),
    },
)
LINK_PROPS_SF_TC = replace(
    SIMPLE_SF_TC,
    test_id="link_props_single_file",
    links=[
        Link(
            "test-link",
            properties={"name": "link-name", "timeout": 10},
            permissions=[],
        )
    ],
    expected_code_assets={
        "simple.py": (FileAsset, "functions/simple.py"),
        "stlv_resources.py": (StringAsset, TEST_LINK_FILE_CONTENT),
    },
    expected_envars={
        "STLV_TEST_LINK_NAME": "link-name",
        "STLV_TEST_LINK_TIMEOUT": 10,
    },
    expected_ide_file=(
        "functions/stlv_resources.py",
        TEST_LINK_FILE_CONTENT_IDE,
    ),
)
LINK2_PROPS_SF_TC = replace(
    LINK_PROPS_SF_TC,
    links=[
        Link(
            "test-link2",
            properties={"name2": "link-name2", "timeout2": 20},
            permissions=[],
        )
    ],
    expected_code_assets={
        "simple.py": (FileAsset, "functions/simple.py"),
        "stlv_resources.py": (StringAsset, TEST_LINK_2_FILE_CONTENT),
    },
    expected_envars={
        "STLV_TEST_LINK2_NAME2": "link-name2",
        "STLV_TEST_LINK2_TIMEOUT2": 20,
    },
    expected_ide_file=(
        "functions/stlv_resources.py",
        TEST_LINK_2_FILE_CONTENT_IDE_SF,
    ),
)
LINK2_PROPS_SF2_TC = replace(
    LINK2_PROPS_SF_TC,
    name="simple2-single-file-function",
    input_handler="functions/simple2.handler",
    expected_handler="simple2.handler",
    expected_code_assets={
        "simple2.py": (FileAsset, "functions/simple2.py"),
        "stlv_resources.py": (StringAsset, TEST_LINK_2_FILE_CONTENT),
    },
    expected_ide_file=(
        "functions/stlv_resources.py",
        TEST_LINK_2_FILE_CONTENT_IDE_SF,
    ),
)
LINK_PROPS_FB_TC = replace(
    SIMPLE_FB_TC,
    test_id="link_props_folder_based",
    links=LINK_PROPS_SF_TC.links,
    expected_code_assets={
        "handler.py": (FileAsset, "functions/folder/handler.py"),
        "stlv_resources.py": (StringAsset, TEST_LINK_FILE_CONTENT),
    },
    expected_envars=LINK_PROPS_SF_TC.expected_envars,
    expected_ide_file=(
        "functions/folder/stlv_resources.py",
        TEST_LINK_FILE_CONTENT_IDE,
    ),
)
LINK2_PROPS_FB_TC = replace(
    LINK_PROPS_FB_TC,
    links=LINK2_PROPS_SF_TC.links,
    expected_code_assets={
        "handler.py": (FileAsset, "functions/folder/handler.py"),
        "stlv_resources.py": (StringAsset, TEST_LINK_2_FILE_CONTENT),
    },
    expected_envars=LINK2_PROPS_SF_TC.expected_envars,
    expected_ide_file=(
        "functions/folder/stlv_resources.py",
        # We use content for single-file because both test folder-based functions
        # refer to the same folder so they share stlv resource file!
        TEST_LINK_2_FILE_CONTENT_IDE_SF,
    ),
)
LINK2_PROPS_FB2_TC = replace(
    LINK_PROPS_FB_TC,
    name="simple2-folder-based-function",
    input_handler="functions/folder2::handler.process",
    links=LINK2_PROPS_SF_TC.links,
    expected_code_assets={
        "handler.py": (FileAsset, "functions/folder2/handler.py"),
        "stlv_resources.py": (StringAsset, TEST_LINK_2_FILE_CONTENT),
    },
    expected_envars=LINK2_PROPS_SF_TC.expected_envars,
    expected_ide_file=(
        "functions/folder2/stlv_resources.py",
        TEST_LINK_2_FILE_CONTENT_IDE_FB,
    ),
)
LINK_PROPS_PERMISSIONS_SF_TC = replace(
    LINK_PROPS_SF_TC,
    test_id="link_props_permissions_single_file",
    links=[
        Link(
            "test-link",
            properties={"name": "link-name", "timeout": 10},
            permissions=[
                AwsPermission(
                    actions=[
                        "dynamodb:Query",
                        "dynamodb:GetItem",
                    ],
                    resources=[
                        "arn:aws:dynamodb:us-east-1:123456789012:table/my-table"
                    ],
                )
            ],
        )
    ],
    expected_policy=[
        {
            "actions": ["dynamodb:Query", "dynamodb:GetItem"],
            "resources": ["arn:aws:dynamodb:us-east-1:123456789012:table/my-table"],
        }
    ],
)


def verify_function_resources(
    pulumi_mocks, project_cwd, function, test_case: FunctionTestCase
):
    # Policy
    policies = pulumi_mocks.created_policies(f"{test_case.name}-Policy")
    if test_case.expected_policy:
        assert len(policies) == 1
        policy_args = policies[0]
        policy_str = json.dumps(test_case.expected_policy)
        assert policy_str == policy_args.inputs["policy"]

    # Role
    roles = pulumi_mocks.created_roles(f"{test_case.name}-role")
    assert len(roles) == 1
    assert roles[0].inputs == {
        "assumeRolePolicy": json.dumps(LAMBDA_ASSUME_ROLE_POLICY)
    }

    # Role attachment
    role_attachments = pulumi_mocks.created_role_policy_attachments()
    assert len(role_attachments) == 1 + (1 if test_case.expected_policy else 0)

    # Find basic execution role attachment by policy ARN
    basic_role_attachment = None
    default_role_attachment = None

    for attachment in role_attachments:
        if (
            attachment.inputs["policyArn"]
            == "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        ):
            basic_role_attachment = attachment
        else:
            default_role_attachment = attachment

    # Verify basic execution role attachment
    assert (
        basic_role_attachment is not None
    ), "BasicExecutionRolePolicyAttachment not found"
    assert (
        basic_role_attachment.name
        == f"{test_case.name}-BasicExecutionRolePolicyAttachment"
    )
    assert basic_role_attachment.inputs["role"] == f"{test_case.name}-role-test-name"

    # Verify policy attachment if it exists
    if test_case.expected_policy:
        assert (
            default_role_attachment is not None
        ), "DefaultRolePolicyAttachment not found"
        assert (
            default_role_attachment.name
            == f"{test_case.name}-DefaultRolePolicyAttachment"
        )
        assert (
            default_role_attachment.inputs["role"] == f"{test_case.name}-role-test-name"
        )

        def assert_attachment_arn(arn):
            assert default_role_attachment.inputs["policyArn"] == arn

        function.resources.policy.arn.apply(assert_attachment_arn)

    # Lambda function resource in created_resources
    functions = pulumi_mocks.created_functions(test_case.name)
    assert len(functions) == 1

    function_args = functions[0]

    def assert_function_role_arn(role_arn):
        assert function_args.inputs["role"] == role_arn

    function.resources.role.arn.apply(assert_function_role_arn)

    assert_function_configuration(
        function_args,
        name=test_case.name,
        handler=test_case.expected_handler,
        envars=test_case.expected_envars,
    )
    assert_function_code(
        function_args, project_cwd, assets=test_case.expected_code_assets
    )

    # Check if stlv_resources.py is created for IDE
    if test_case.expected_ide_file:
        file_path = project_cwd / test_case.expected_ide_file[0]
        assert file_path.exists()
        assert file_path.read_text() == test_case.expected_ide_file[1]


@pulumi.runtime.test
def test_function_properties(pulumi_mocks, project_cwd):
    """
    Checks if Function's properties return proper values.
    It's separate test as it shouldn't change based on function configuration.
    """
    function = Function(
        "simple-single-file-function", handler="functions/simple.handler"
    )
    function_resource = function._create_resource()

    def check_properties(args):
        name, invoke_arn, resource_name, resource_invoke_arn = args
        assert name == resource_name
        assert invoke_arn == resource_invoke_arn

    pulumi.Output.all(
        function.resource_name,
        function.invoke_arn,
        function_resource.name,
        function_resource.invoke_arn,
    ).apply(check_properties)


@pytest.mark.parametrize(
    "test_case",
    [
        SIMPLE_SF_TC,
        SIMPLE_FB_TC,
        ROUTING_SF_TC,
        ROUTING_FB_TC,
        LINK_PROPS_SF_TC,
        LINK_PROPS_FB_TC,
        LINK_PROPS_PERMISSIONS_SF_TC,
        replace(
            LINK_PROPS_FB_TC,
            test_id="link_props_permissions_folder_based",
            links=LINK_PROPS_PERMISSIONS_SF_TC.links,
            expected_policy=LINK_PROPS_PERMISSIONS_SF_TC.expected_policy,
        ),
    ],
    ids=lambda test_case: test_case.test_id,
)
@pulumi.runtime.test
def test_function__(pulumi_mocks, project_cwd, test_case):

    function = Function(
        test_case.name, handler=test_case.input_handler, links=test_case.links
    )
    if test_case.extra_assets_map:
        FunctionAssetsRegistry.add(function, test_case.extra_assets_map)
    function_resource = function._create_resource()

    def check_resources(args):
        # This callback will only run after all outputs are resolved
        # Including the policy arn which is used in the role policy attachment
        function_id, role_id, policy_output = args
        verify_function_resources(pulumi_mocks, project_cwd, function, test_case)

    # Create a dependency on all critical outputs
    # This ensures attachments are completed before we verify resources
    pulumi.Output.all(
        function_resource.id,
        function.resources.role.id,
        function.resources.policy,  # This will resolve to None or the policy
    ).apply(check_resources)


@pytest.mark.parametrize(
    "test_case_set",
    [
        [LINK_PROPS_SF_TC, LINK2_PROPS_SF_TC],
        [LINK_PROPS_SF_TC, LINK2_PROPS_SF2_TC],
        [LINK_PROPS_FB_TC, LINK2_PROPS_FB_TC],
        [LINK_PROPS_FB_TC, LINK2_PROPS_FB2_TC],
    ],
    ids=[
        "ide_files_two_single_file_from_same_file",
        "ide_files_two_single_file_from_different_files",
        "ide_files_two_folder_based_from_same_folder",
        "ide_files_two_folder_based_from_different_folders",
    ],
)
@pulumi.runtime.test
def test_functions_multiple__(pulumi_mocks, project_cwd, test_case_set):
    def create_and_verify_function(test_case):
        function = Function(
            test_case.name, handler=test_case.input_handler, links=test_case.links
        )
        function_resource = function._create_resource()
        return function, function_resource

    def process_test_cases(remaining_test_cases):
        if not remaining_test_cases:
            return

        test_case = remaining_test_cases[0]
        function, function_resource = create_and_verify_function(test_case)

        def check_resources(args):
            # This callback will only run after all outputs are resolved
            function_id, role_id, policy_output = args
            verify_function_resources(pulumi_mocks, project_cwd, function, test_case)
            pulumi_mocks.created_resources = []
            # Process next test case if any remain
            process_test_cases(remaining_test_cases[1:])

        # Create a dependency on all critical outputs
        # This ensures attachments are completed before we verify resources
        pulumi.Output.all(
            function_resource.id,
            function.resources.role.id,
            function.resources.policy,  # This will resolve to None or the policy
        ).apply(check_resources)

    process_test_cases(test_case_set)
