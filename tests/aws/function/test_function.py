"""
In these tests we tests that Stelvio creates proper Pulumi resources.

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
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, patch

import pulumi
import pytest
from pulumi import (
    Asset,
    AssetArchive,
    FileArchive,
    FileAsset,
    RemoteAsset,
    StringAsset,
)
from pulumi.runtime import MockResourceArgs, set_mocks

from stelvio.aws._packaging.dependencies import RequirementsSpec
from stelvio.aws.function import Function
from stelvio.aws.function.constants import (
    DEFAULT_ARCHITECTURE,
    DEFAULT_MEMORY,
    DEFAULT_RUNTIME,
    DEFAULT_TIMEOUT,
)
from stelvio.aws.function.dependencies import _FUNCTION_CACHE_SUBDIR
from stelvio.aws.function.function import FunctionAssetsRegistry
from stelvio.aws.layer import Layer
from stelvio.aws.permission import AwsPermission
from stelvio.aws.types import AwsArchitecture, AwsLambdaRuntime
from stelvio.link import Link, Linkable

from ..pulumi_mocks import PulumiTestMocks

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
    test_link: Final[TestLinkResource] = TestLinkResource()
\n
Resources: Final = LinkedResources()"""

TEST_LINK_FILE_CONTENT_IDE = TEST_LINK_FILE_CONTENT

TEST_LINK_2_FILE_CONTENT = f"""{LINK_FILE_IMPORTS}
\n\n{TEST_LINK_2_DATACLASS_TXT}\n
@dataclass(frozen=True)
class LinkedResources:
    test_link2: Final[TestLink2Resource] = TestLink2Resource()
\n
Resources: Final = LinkedResources()"""

TEST_LINK_2_FILE_CONTENT_IDE_SF = f"""{LINK_FILE_IMPORTS}
\n\n{TEST_LINK_DATACLASS_TXT}\n\n{TEST_LINK_2_DATACLASS_TXT}\n
@dataclass(frozen=True)
class LinkedResources:
    test_link: Final[TestLinkResource] = TestLinkResource()
    test_link2: Final[TestLink2Resource] = TestLink2Resource()
\n
Resources: Final = LinkedResources()"""

TEST_LINK_2_FILE_CONTENT_IDE_FB = TEST_LINK_2_FILE_CONTENT


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


def delete_files(directory, filename):
    """Helper to clean up generated files (like stlv_resources.py)."""
    file_path: Path
    for file_path in Path(directory).rglob(filename):
        file_path.unlink(missing_ok=True)


# Test prefix
TP = "test-test-"


@pytest.fixture
def project_cwd(monkeypatch, pytestconfig, tmp_path):
    from stelvio.project import get_project_root

    get_project_root.cache_clear()
    rootpath = pytestconfig.rootpath
    source_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    temp_project_dir = tmp_path / "sample_project_copy"

    shutil.copytree(source_project_dir, temp_project_dir, dirs_exist_ok=True)
    monkeypatch.chdir(temp_project_dir)
    # with patch("stelvio.aws.function.get_project_root", return_value=temp_project_dir):

    return temp_project_dir


# @pytest.fixture(autouse=True)
# def project_cwd(monkeypatch, pytestconfig):
#     rootpath = pytestconfig.rootpath
#     test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
#     print(f"Data directory: {test_project_dir}")
#     monkeypatch.chdir(test_project_dir)
#     yield test_project_dir
#     delete_files(test_project_dir, "stlv_resources.py")


@dataclass
class FunctionTestCase:
    test_id: str
    name: str
    input_handler: str
    expected_handler: str
    expected_code_assets: dict[str, tuple[type[Asset], str]]
    extra_assets_map: dict[str, Asset] | None = None
    links: list[Link | Linkable] = field(default_factory=list)
    expected_envars: dict[str, str | int] = field(default_factory=dict)
    expected_policy: list[dict[str, list[str]]] | None = None
    expected_ide_file: tuple[str, str] | None = None
    layers: list[Callable[[], Layer]] | None = None
    expected_layers: list[str] | None = None
    requirements: list[str] | str | bool | None = None
    create_default_requirements_at: str | None = None
    runtime: AwsLambdaRuntime | None = None
    arch: AwsArchitecture | None = None


def assert_function_configuration(function_args, test_case: FunctionTestCase):
    # Check lambda configuration
    assert function_args.name == TP + test_case.name
    assert function_args.inputs["handler"] == test_case.expected_handler
    assert function_args.inputs["runtime"] == test_case.runtime or DEFAULT_RUNTIME
    assert function_args.inputs["architectures"] == [test_case.arch or DEFAULT_ARCHITECTURE]
    assert function_args.inputs["memorySize"] == DEFAULT_MEMORY
    assert function_args.inputs["timeout"] == DEFAULT_TIMEOUT
    assert function_args.inputs["environment"] == {"variables": test_case.expected_envars}


def assert_function_layers(function_args, expected_layers_arns: list[str] | None):
    actual_layers_output = function_args.inputs.get("layers")

    if expected_layers_arns is None:
        assert actual_layers_output is None
        return

    assert actual_layers_output is not None
    assert isinstance(actual_layers_output, list)
    assert sorted(actual_layers_output) == sorted(expected_layers_arns)


def assert_function_code(
    function_args: MockResourceArgs,
    project_cwd,
    assets: dict[str, tuple[type[Asset], str]],
    expected_dependencies_path: Path | None,
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

    if not expected_dependencies_path:
        return
    # Check dependency archive
    expected_depencencies_key = ""  # direct lambda dependencies are at root
    assert expected_depencencies_key in code.assets
    dependencies_archive_asset = code.assets[expected_depencencies_key]
    assert isinstance(dependencies_archive_asset, FileArchive)
    assert dependencies_archive_asset.path == str(expected_dependencies_path)


def expected_layer_arn(layer_name: str) -> str:
    return f"arn:aws:lambda:us-east-1:123456789012:layer:{TP + layer_name}-test-name:1"


def create_layer(name=None, runtime=None, arch=None):
    return Layer(
        name=name or "mock-layer-1",
        requirements=["dummy-req"],
        runtime=runtime or DEFAULT_RUNTIME,
        architecture=arch or DEFAULT_ARCHITECTURE,
    )


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
    links=[Link("test-link", properties={"name": "link-name", "timeout": 10}, permissions=[])],
    expected_code_assets={
        "simple.py": (FileAsset, "functions/simple.py"),
        "stlv_resources.py": (StringAsset, TEST_LINK_FILE_CONTENT),
    },
    expected_envars={"STLV_TEST_LINK_NAME": "link-name", "STLV_TEST_LINK_TIMEOUT": 10},
    expected_ide_file=("functions/stlv_resources.py", TEST_LINK_FILE_CONTENT_IDE),
)
LINK2_PROPS_SF_TC = replace(
    LINK_PROPS_SF_TC,
    links=[Link("test-link2", properties={"name2": "link-name2", "timeout2": 20}, permissions=[])],
    expected_code_assets={
        "simple.py": (FileAsset, "functions/simple.py"),
        "stlv_resources.py": (StringAsset, TEST_LINK_2_FILE_CONTENT),
    },
    expected_envars={"STLV_TEST_LINK2_NAME2": "link-name2", "STLV_TEST_LINK2_TIMEOUT2": 20},
    expected_ide_file=("functions/stlv_resources.py", TEST_LINK_2_FILE_CONTENT_IDE_SF),
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
    expected_ide_file=("functions/stlv_resources.py", TEST_LINK_2_FILE_CONTENT_IDE_SF),
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
    expected_ide_file=("functions/folder/stlv_resources.py", TEST_LINK_FILE_CONTENT_IDE),
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
    expected_ide_file=("functions/folder2/stlv_resources.py", TEST_LINK_2_FILE_CONTENT_IDE_FB),
)
REQUIREMENTS_LIST_SF_TC = replace(
    SIMPLE_SF_TC, test_id="requirements_as_list_single_file", requirements=["boto3", "requests"]
)
REQUIREMENTS_DEFAULT_FILE_SF_TC = replace(
    SIMPLE_SF_TC,
    test_id="requirements_default_file_single_file",
    create_default_requirements_at="functions/requirements.txt",
)
REQUIREMENTS_DEFAULT_FILE_OFF_SF_TC = replace(
    SIMPLE_SF_TC,
    test_id="requirements_default_file_off_single_file",
    requirements=False,
    create_default_requirements_at="functions/requirements.txt",
)
REQUIREMENTS_DEFAULT_FILE_BUT_CUSTOM_FILE_SF_TC = replace(
    SIMPLE_SF_TC,
    test_id="requirements_default_file_but_use_custom_file_single_file",
    requirements="functions/custom_requirements.txt",
    create_default_requirements_at="functions/requirements.txt",
)
REQUIREMENTS_DEFAULT_FILE_BUT_CUSTOM_LIST_SF_TC = replace(
    SIMPLE_SF_TC,
    test_id="requirements_default_file_but_custom_list_single_file",
    requirements=["boto3", "requests"],
    create_default_requirements_at="functions/requirements.txt",
)
REQUIREMENTS_LIST_CUSTOM_RUNTIME_ARCH_SF_TC = replace(
    SIMPLE_SF_TC,
    test_id="requirements_as_list_custom_runtime_and_arch_single_file",
    requirements=["boto3", "requests"],
    runtime="python3.13",
    arch="arm64",
)
# --
REQUIREMENTS_LIST_FB_TC = replace(
    SIMPLE_FB_TC, test_id="requirements_as_list_folder_based", requirements=["boto3", "requests"]
)
REQUIREMENTS_DEFAULT_FILE_FB_TC = replace(
    SIMPLE_FB_TC,
    test_id="requirements_default_file_folder_based",
    create_default_requirements_at="functions/folder/requirements.txt",
)
REQUIREMENTS_DEFAULT_FILE_OFF_FB_TC = replace(
    SIMPLE_FB_TC,
    test_id="requirements_default_file_off_folder_based",
    requirements=False,
    create_default_requirements_at="functions/folder/requirements.txt",
)
REQUIREMENTS_DEFAULT_FILE_BUT_CUSTOM_FILE_FB_TC = replace(
    SIMPLE_FB_TC,
    test_id="requirements_default_file_but_use_custom_file_folder_based",
    requirements="functions/custom_requirements.txt",
    create_default_requirements_at="functions/folder/requirements.txt",
)
REQUIREMENTS_DEFAULT_FILE_BUT_CUSTOM_LIST_FB_TC = replace(
    SIMPLE_FB_TC,
    test_id="requirements_default_file_but_custom_list_folder_based",
    requirements=["boto3", "requests"],
    create_default_requirements_at="functions/folder/requirements.txt",
)
REQUIREMENTS_LIST_CUSTOM_RUNTIME_ARCH_FB_TC = replace(
    SIMPLE_FB_TC,
    test_id="requirements_as_list_custom_runtime_and_arch_folder_based",
    requirements=["boto3", "requests"],
    runtime="python3.13",
    arch="arm64",
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
                    actions=["dynamodb:Query", "dynamodb:GetItem"],
                    resources=["arn:aws:dynamodb:us-east-1:123456789012:table/my-table"],
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

ONE_LAYER_SF_TC = replace(
    SIMPLE_SF_TC,
    test_id="one_layer_single_file",
    layers=[create_layer],
    expected_layers=[expected_layer_arn("mock-layer-1")],
)

MULTI_LAYER_FB_TC = replace(
    SIMPLE_FB_TC,
    test_id="multi_layer_folder_based",
    layers=[create_layer, partial(create_layer, name="mock-layer-2")],
    expected_layers=[
        expected_layer_arn("mock-layer-1"),
        expected_layer_arn("mock-layer-2"),
    ],
)


def _assert_iam_policy(pulumi_mocks, test_case: FunctionTestCase):
    """Verify the IAM policy creation."""
    policies = pulumi_mocks.created_policies(f"{TP + test_case.name}-p")
    if test_case.expected_policy:
        assert len(policies) == 1, "Expected 1 policy to be created"
        policy_args = policies[0]
        policy_str = json.dumps(test_case.expected_policy)
        assert policy_str == policy_args.inputs["policy"], "Policy content mismatch"
    else:
        assert len(policies) == 0, "Expected no policy to be created"


def _assert_iam_role(pulumi_mocks, test_case: FunctionTestCase):
    """Verify the IAM role creation."""
    roles = pulumi_mocks.created_roles(f"{TP + test_case.name}-r")
    assert len(roles) == 1, "Expected 1 role to be created"
    assert roles[0].inputs == {"assumeRolePolicy": json.dumps(LAMBDA_ASSUME_ROLE_POLICY)}


def _assert_role_attachments(pulumi_mocks, test_case: FunctionTestCase, function: Function):
    """Verify the IAM role policy attachments."""
    role_attachments = pulumi_mocks.created_role_policy_attachments()
    expected_attachments = 1 + (1 if test_case.expected_policy else 0)
    assert len(role_attachments) == expected_attachments

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
    assert basic_role_attachment is not None
    assert basic_role_attachment.name == f"{TP + test_case.name}-basic-execution-r-p-attachment"
    assert basic_role_attachment.inputs["role"] == f"{TP + test_case.name}-r-test-name"

    if test_case.expected_policy:
        assert default_role_attachment is not None
        assert default_role_attachment.name == f"{TP + test_case.name}-default-r-p-attachment"
        assert default_role_attachment.inputs["role"] == f"{TP + test_case.name}-r-test-name"

        def assert_attachment_arn(policy_arn):
            assert default_role_attachment.inputs["policyArn"] == policy_arn

        assert function.resources.policy is not None, "Function policy resource should exist"
        function.resources.policy.arn.apply(assert_attachment_arn)


def _assert_lambda_function(
    pulumi_mocks,
    project_cwd,
    function: Function,
    test_case: FunctionTestCase,
    expected_dependencies_path: Path | None,
):
    functions = pulumi_mocks.created_functions(TP + test_case.name)
    assert len(functions) == 1
    function_args = functions[0]

    def assert_function_role_arn(role_arn):
        assert function_args.inputs["role"] == role_arn

    function.resources.role.arn.apply(assert_function_role_arn)

    assert_function_configuration(function_args, test_case)
    assert_function_code(
        function_args, project_cwd, test_case.expected_code_assets, expected_dependencies_path
    )
    assert_function_layers(function_args, expected_layers_arns=test_case.expected_layers)


def _assert_ide_file(project_cwd, test_case: FunctionTestCase):
    if test_case.expected_ide_file:
        file_path = project_cwd / test_case.expected_ide_file[0]
        assert file_path.exists(), f"IDE resource file not found at {file_path}"
        assert file_path.read_text() == test_case.expected_ide_file[1], "IDE file content mismatch"


def verify_function_resources(
    pulumi_mocks,
    project_cwd,
    function,
    test_case: FunctionTestCase,
    expected_dependencies_path: Path | None,
):
    _assert_iam_policy(pulumi_mocks, test_case)
    _assert_iam_role(pulumi_mocks, test_case)
    _assert_role_attachments(pulumi_mocks, test_case, function)
    _assert_lambda_function(
        pulumi_mocks, project_cwd, function, test_case, expected_dependencies_path
    )
    _assert_ide_file(project_cwd, test_case)


@pulumi.runtime.test
def test_function_properties(pulumi_mocks, project_cwd):
    function = Function("simple-single-file-function", handler="functions/simple.handler")
    function_resource = function.resources.function

    def check_properties(args):
        name, invoke_arn, resource_name, resource_invoke_arn = args
        assert name == resource_name
        assert invoke_arn == resource_invoke_arn

    pulumi.Output.all(
        function.function_name,
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
        REQUIREMENTS_LIST_SF_TC,
        REQUIREMENTS_LIST_FB_TC,
        REQUIREMENTS_DEFAULT_FILE_SF_TC,
        REQUIREMENTS_DEFAULT_FILE_FB_TC,
        REQUIREMENTS_DEFAULT_FILE_OFF_SF_TC,
        REQUIREMENTS_DEFAULT_FILE_OFF_FB_TC,
        REQUIREMENTS_DEFAULT_FILE_BUT_CUSTOM_FILE_SF_TC,
        REQUIREMENTS_DEFAULT_FILE_BUT_CUSTOM_FILE_FB_TC,
        REQUIREMENTS_DEFAULT_FILE_BUT_CUSTOM_LIST_SF_TC,
        REQUIREMENTS_DEFAULT_FILE_BUT_CUSTOM_LIST_FB_TC,
        REQUIREMENTS_LIST_CUSTOM_RUNTIME_ARCH_SF_TC,
        REQUIREMENTS_LIST_CUSTOM_RUNTIME_ARCH_FB_TC,
        ONE_LAYER_SF_TC,
        MULTI_LAYER_FB_TC,
    ],
    ids=lambda test_case: test_case.test_id,
)
@pulumi.runtime.test
def test_function__(
    mock_get_or_install_dependencies_function,
    mock_get_or_install_dependencies_layer,
    pulumi_mocks,
    project_cwd,
    test_case,
):
    # Arrange
    function = Function(
        test_case.name,
        handler=test_case.input_handler,
        links=test_case.links,
        layers=[layer() for layer in test_case.layers] if test_case.layers else None,
        requirements=test_case.requirements,
        runtime=test_case.runtime,
        architecture=test_case.arch,
    )
    if test_case.extra_assets_map:
        FunctionAssetsRegistry.add(function, test_case.extra_assets_map)
    if test_case.create_default_requirements_at:
        (project_cwd / test_case.create_default_requirements_at).touch()
    if isinstance(test_case.requirements, str):
        (project_cwd / test_case.requirements).touch()

    # Assert
    def check_resources(_):
        requirements = test_case.requirements
        expect_requirements = bool(requirements) or (
            test_case.create_default_requirements_at and requirements is not False
        )
        dependencies_path = (
            mock_get_or_install_dependencies_function.return_value if expect_requirements else None
        )
        verify_function_resources(
            pulumi_mocks, project_cwd, function, test_case, dependencies_path
        )
        if expect_requirements:
            mock_get_or_install_dependencies_function.assert_called_once_with(
                requirements_source=RequirementsSpec(
                    content="\n".join(requirements) if isinstance(requirements, list) else None,
                    path_from_root=Path(requirements)
                    if isinstance(requirements, str)
                    else (
                        Path(test_case.create_default_requirements_at)
                        if test_case.create_default_requirements_at
                        and requirements is not False
                        and not isinstance(requirements, list)
                        else None
                    ),
                ),
                runtime=test_case.runtime or DEFAULT_RUNTIME,
                architecture=test_case.arch or DEFAULT_ARCHITECTURE,
                project_root=project_cwd,
                log_context=f"Function: {test_case.input_handler}",
                cache_subdirectory=_FUNCTION_CACHE_SUBDIR,
            )
        else:
            mock_get_or_install_dependencies_function.assert_not_called()

    # Act
    function.invoke_arn.apply(check_resources)


@pytest.mark.parametrize(
    "test_case_set",
    [
        [LINK_PROPS_SF_TC, replace(LINK2_PROPS_SF_TC, name=LINK2_PROPS_SF_TC.name + "2nd")],
        [LINK_PROPS_SF_TC, LINK2_PROPS_SF2_TC],
        [LINK_PROPS_FB_TC, replace(LINK2_PROPS_FB_TC, name=LINK2_PROPS_FB_TC.name + "2nd")],
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
        function = Function(test_case.name, handler=test_case.input_handler, links=test_case.links)
        function_resource = function.resources.function
        return function, function_resource

    def process_test_cases(remaining_test_cases):
        if not remaining_test_cases:
            return

        test_case = remaining_test_cases[0]
        function, function_resource = create_and_verify_function(test_case)

        def check_resources(_):
            verify_function_resources(pulumi_mocks, project_cwd, function, test_case, None)
            pulumi_mocks.created_resources = []
            process_test_cases(remaining_test_cases[1:])

        function_resource.id.apply(check_resources)

    process_test_cases(test_case_set)


@pulumi.runtime.test
@patch("stelvio.aws.function.function.safe_name")
def test_function_uses_safe_name(mock_safe_name, pulumi_mocks, project_cwd):
    # Arrange - Mock safe_name to return a specific value
    mocked_name = "test-test-mocked-safe-function-name"
    mock_safe_name.return_value = mocked_name

    # Act - Create function
    function = Function("my-function", handler="functions/simple.handler")
    _ = function.resources

    # Assert
    def check_safe_name_usage(_):
        # Find the call for this specific Lambda function
        # safe_name signature: (prefix, name, max_length, suffix="", pulumi_suffix=8)
        lambda_calls = [
            call
            for call in mock_safe_name.call_args_list
            if call.args[1] == "my-function"  # function name
            and call.args[2] == 64  # max_length for Lambda
            and (len(call.args) < 4 or call.args[3] == "")  # no suffix
        ]

        assert len(lambda_calls) == 1, "safe_name should be called once for Lambda function"

        # Verify the Lambda function was created with the mocked safe_name return value
        functions = pulumi_mocks.created_functions()
        assert len(functions) == 1
        assert functions[0].name == mocked_name, (
            f"Lambda function should use safe_name return value. "
            f"Expected: {mocked_name}, Got: {functions[0].name}"
        )

    function.resources.function.id.apply(check_safe_name_usage)


@pytest.mark.parametrize(
    ("opts", "error_type", "error_match"),
    [
        ({"requirements": "nonexistent.txt"}, FileNotFoundError, "Requirements file not found"),
        ({"requirements": "functions"}, ValueError, "Requirements path is not a file"),
        (
            {"requirements": "../outside-folder/file.txt"},
            ValueError,
            "which is outside the project root",
        ),
    ],
    ids=[
        "requirements_path_does_not_exist",
        "requirements_path_is_folder",
        "requirements_path_outside_of_project_root",
    ],
)
@pulumi.runtime.test
def test_function_raises_when__(project_cwd, pulumi_mocks, opts, error_type, error_match):
    # Arrange
    outside_folder = project_cwd / "../outside-folder"
    outside_folder.mkdir(parents=True)
    outside_file = outside_folder / "file.txt"
    outside_file.touch()
    # Act & Assert
    with pytest.raises(error_type, match=error_match):
        _ = Function("my-function", handler="functions/simple.handler", **opts).resources


# Bridge Mode Tests
BRIDGE_MODE_SF_TC = replace(
    SIMPLE_SF_TC,
    test_id="bridge_mode_single_file",
    expected_handler="function_stub.handler",
    expected_code_assets={"function_stub.py": (StringAsset, "stub-content")},
)

BRIDGE_MODE_FB_TC = replace(
    SIMPLE_FB_TC,
    test_id="bridge_mode_folder_based",
    expected_handler="function_stub.handler",
    expected_code_assets={"function_stub.py": (StringAsset, "stub-content")},
)

BRIDGE_MODE_WITH_LINKS_SF_TC = replace(
    LINK_PROPS_SF_TC,
    test_id="bridge_mode_with_links_single_file",
    expected_handler="function_stub.handler",
    expected_code_assets={"function_stub.py": (StringAsset, "stub-content")},
    expected_envars={
        **LINK_PROPS_SF_TC.expected_envars,
        "STLV_APPSYNC_REALTIME": "wss://test-realtime.appsync.amazonaws.com",
        "STLV_APPSYNC_HTTP": "https://test-http.appsync.amazonaws.com",
        "STLV_APPSYNC_API_KEY": "test-api-key-123",
        "STLV_APP_NAME": "test",
        "STLV_STAGE": "test",
        "STLV_FUNCTION_NAME": LINK_PROPS_SF_TC.name,
    },
)

BRIDGE_MODE_WITH_LAYERS_SF_TC = replace(
    ONE_LAYER_SF_TC,
    test_id="bridge_mode_with_layers_single_file",
    expected_handler="function_stub.handler",
    expected_code_assets={"function_stub.py": (StringAsset, "stub-content")},
)


def _assert_bridge_env_vars(function_args, test_case: FunctionTestCase):
    """Verify bridge-specific environment variables are present."""
    env_vars = function_args.inputs["environment"]["variables"]

    # Check required bridge env vars
    assert "STLV_APPSYNC_REALTIME" in env_vars
    assert "STLV_APPSYNC_HTTP" in env_vars
    assert "STLV_APPSYNC_API_KEY" in env_vars
    assert "STLV_APP_NAME" in env_vars
    assert "STLV_STAGE" in env_vars
    assert "STLV_FUNCTION_NAME" in env_vars
    assert "STLV_DEV_ENDPOINT_ID" in env_vars

    # Verify values
    assert env_vars["STLV_APPSYNC_REALTIME"] == "wss://test-realtime.appsync.amazonaws.com"
    assert env_vars["STLV_APPSYNC_HTTP"] == "https://test-http.appsync.amazonaws.com"
    assert env_vars["STLV_APPSYNC_API_KEY"] == "test-api-key-123"
    assert env_vars["STLV_APP_NAME"] == "test"
    assert env_vars["STLV_STAGE"] == "test"
    assert env_vars["STLV_FUNCTION_NAME"] == test_case.name


@pytest.mark.parametrize(
    "test_case",
    [
        BRIDGE_MODE_SF_TC,
        BRIDGE_MODE_FB_TC,
        BRIDGE_MODE_WITH_LINKS_SF_TC,
        BRIDGE_MODE_WITH_LAYERS_SF_TC,
    ],
    ids=lambda test_case: test_case.test_id,
)
@pulumi.runtime.test
def test_function_bridge_mode__(
    mock_get_or_install_dependencies_function,
    mock_get_or_install_dependencies_layer,
    pulumi_mocks,
    project_cwd,
    test_case,
):
    """Test that functions are created correctly in bridge mode."""
    from stelvio.bridge.remote.infrastructure import AppSyncResource
    from stelvio.context import AppContext, _ContextStore, context

    # Create a new context with bridge_mode enabled
    ctx = context()
    bridge_ctx = AppContext(
        name=ctx.name,
        env=ctx.env,
        aws=ctx.aws,
        dns=ctx.dns,
        bridge_mode=True,
    )
    # Clear and set new context with bridge_mode=True
    _ContextStore.clear()
    _ContextStore.set(bridge_ctx)

    # Mock AppSync discovery
    mock_appsync_resource = AppSyncResource(
        api_id="test-api-id",
        http_endpoint="https://test-http.appsync.amazonaws.com",
        realtime_endpoint="wss://test-realtime.appsync.amazonaws.com",
        api_key="test-api-key-123",
    )

    with (
        patch("stelvio.aws.function.function.discover_or_create_appsync") as mock_discover,
        patch(
            "stelvio.aws.function.function._create_lambda_bridge_archive"
        ) as mock_bridge_archive,
    ):
        # Setup mocks
        mock_discover.return_value = mock_appsync_resource
        mock_bridge_archive.return_value = AssetArchive(
            {"function_stub.py": StringAsset("stub-content")}
        )

        # Create function with required config
        function_kwargs = {
            "handler": test_case.input_handler,
        }
        if test_case.links:
            function_kwargs["links"] = test_case.links
        if test_case.layers:
            function_kwargs["layers"] = [layer() for layer in test_case.layers]

        function = Function(test_case.name, **function_kwargs)
        _ = function.resources

        # Verify AppSync was discovered
        mock_discover.assert_called_once()

        # Verify bridge archive was created
        mock_bridge_archive.assert_called_once()

        # Create assertion function to be called after resources are created
        def check_resources(_):
            # Verify function configuration
            functions = pulumi_mocks.created_functions(TP + test_case.name)
            assert len(functions) == 1
            function_args = functions[0]

            # Check handler is the stub handler
            assert function_args.inputs["handler"] == "function_stub.handler"

            # Check bridge environment variables
            _assert_bridge_env_vars(function_args, test_case)

            # Verify code uses bridge archive
            code: AssetArchive = function_args.inputs["code"]
            assert "function_stub.py" in code.assets
            assert isinstance(code.assets["function_stub.py"], StringAsset)

            # If test case has layers, verify they're still applied
            if test_case.expected_layers:
                assert_function_layers(function_args, test_case.expected_layers)

        # Apply assertions after function resources are created
        function.invoke_arn.apply(check_resources)


@pulumi.runtime.test
def test_function_bridge_mode_registers_handler(project_cwd, pulumi_mocks):
    """Test that function registers itself with WebsocketHandlers in bridge mode."""
    from stelvio.bridge.local.handlers import WebsocketHandlers
    from stelvio.bridge.remote.infrastructure import AppSyncResource

    mock_appsync_resource = AppSyncResource(
        api_id="test-api-id",
        http_endpoint="https://test-http.appsync.amazonaws.com",
        realtime_endpoint="wss://test-realtime.appsync.amazonaws.com",
        api_key="test-api-key-123",
    )

    # Clear handlers before test
    WebsocketHandlers._handlers.clear()

    with (
        patch("stelvio.aws.function.function.discover_or_create_appsync") as mock_discover,
        patch(
            "stelvio.aws.function.function._create_lambda_bridge_archive"
        ) as mock_bridge_archive,
        patch("stelvio.aws.function.function.context") as mock_context,
    ):
        # Setup mocks
        mock_discover.return_value = mock_appsync_resource
        mock_bridge_archive.return_value = AssetArchive(
            {"function_stub.py": StringAsset("stub-content")}
        )

        # Mock context to return bridge_mode=True
        mock_ctx = MagicMock()
        mock_ctx.bridge_mode = True
        mock_ctx.name = "test"
        mock_ctx.env = "test"
        mock_ctx.prefix.return_value = TP
        mock_ctx.aws.region = "us-east-1"
        mock_ctx.aws.profile = None
        mock_context.return_value = mock_ctx

        # Create function
        function = Function("test-function", handler="functions/simple.handler")

        # Create check function
        def check_registration(_):
            # Verify handler was registered
            assert len(WebsocketHandlers._handlers) == 1
            assert function in WebsocketHandlers._handlers

        # Apply check after function resources are created
        function.invoke_arn.apply(check_registration)


@pulumi.runtime.test
def test_function_bridge_mode_generates_endpoint_id(project_cwd, pulumi_mocks):
    """Test that function generates a unique endpoint ID in bridge mode."""
    from stelvio.bridge.remote.infrastructure import AppSyncResource

    mock_appsync_resource = AppSyncResource(
        api_id="test-api-id",
        http_endpoint="https://test-http.appsync.amazonaws.com",
        realtime_endpoint="wss://test-realtime.appsync.amazonaws.com",
        api_key="test-api-key-123",
    )

    with (
        patch("stelvio.aws.function.function.discover_or_create_appsync") as mock_discover,
        patch(
            "stelvio.aws.function.function._create_lambda_bridge_archive"
        ) as mock_bridge_archive,
        patch("stelvio.aws.function.function.context") as mock_context,
    ):
        # Setup mocks
        mock_discover.return_value = mock_appsync_resource
        mock_bridge_archive.return_value = AssetArchive(
            {"function_stub.py": StringAsset("stub-content")}
        )

        # Mock context to return bridge_mode=True
        mock_ctx = MagicMock()
        mock_ctx.bridge_mode = True
        mock_ctx.name = "test"
        mock_ctx.env = "test"
        mock_ctx.prefix.return_value = TP
        mock_ctx.aws.region = "us-east-1"
        mock_ctx.aws.profile = None
        mock_context.return_value = mock_ctx

        # Create two functions with different names to test unique endpoint ID generation
        function1 = Function("test-function-1", handler="functions/simple.handler")
        function2 = Function("test-function-2", handler="functions/simple.handler")

        # Create check function
        def check_endpoint_ids(_):
            # Get endpoint IDs
            endpoint_id_1 = function1._dev_endpoint_id
            endpoint_id_2 = function2._dev_endpoint_id

            # Verify both have endpoint IDs
            assert endpoint_id_1 is not None
            assert endpoint_id_2 is not None

            # Verify they are different (unique)
            assert endpoint_id_1 != endpoint_id_2

        # Apply check after function resources are created
        pulumi.Output.all(function1.invoke_arn, function2.invoke_arn).apply(check_endpoint_ids)
