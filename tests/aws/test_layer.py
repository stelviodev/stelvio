import logging
import re
import shutil
from pathlib import Path
from unittest.mock import patch

import pulumi
import pytest
from pulumi import AssetArchive, FileArchive

from stelvio.aws._packaging.dependencies import RequirementsSpec
from stelvio.aws.function.constants import DEFAULT_ARCHITECTURE, DEFAULT_RUNTIME
from stelvio.aws.layer import _LAYER_CACHE_SUBDIR, Layer

from ..conftest import TP

logger = logging.getLogger(__name__)


# Layer tests need a special project_cwd fixture that patches get_project_root
# differently from the shared fixture, so we keep it local.
@pytest.fixture
def project_cwd(monkeypatch, pytestconfig, tmp_path):
    rootpath = pytestconfig.rootpath
    source_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    temp_project_dir = tmp_path / "sample_project_copy"

    shutil.copytree(source_project_dir, temp_project_dir, dirs_exist_ok=True)
    monkeypatch.chdir(temp_project_dir)

    with patch("stelvio.aws.layer.get_project_root", return_value=temp_project_dir):
        yield temp_project_dir


@pytest.fixture
def mock_cache_fs(tmp_path, monkeypatch):
    dot_stelvio = tmp_path / ".stelvio"
    layer_cache_base = dot_stelvio / "lambda_dependencies" / _LAYER_CACHE_SUBDIR
    layer_cache_base.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "stelvio.aws._packaging.dependencies._get_lambda_dependencies_dir",
        lambda subdir: dot_stelvio / "lambda_dependencies" / subdir,
    )
    monkeypatch.setattr("stelvio.project.get_dot_stelvio_dir", lambda: dot_stelvio)

    return layer_cache_base


@pytest.mark.parametrize(
    ("code", "requirements", "arch", "runtime"),
    [
        ("src/my_layer_code", None, None, None),
        (None, ["requests", "boto3"], None, None),
        (None, "src/layer_requirements.txt", None, None),
        ("src/my_layer_code", ["requests", "boto3"], None, None),
        ("src/my_layer_code", "src/layer_requirements.txt", "python3.13", "arm64"),
    ],
    ids=[
        "code_only",
        "requirements_only_as_list",
        "requirements_only_as_file",
        "code_and_requirements_as_list",
        "code_and_requirements_as_file_custom_runtime_and_arch",
    ],
)
@pulumi.runtime.test
def test_layer_with__(  # noqa: PLR0913
    pulumi_mocks,
    project_cwd,
    mock_cache_fs,
    mock_get_or_install_dependencies_layer,
    code,
    requirements,
    arch,
    runtime,
):
    # Arrange
    layer_name = "my-layer"
    if isinstance(requirements, str):
        requirements_abs = project_cwd / requirements
        requirements_abs.parent.mkdir(parents=True, exist_ok=True)
        requirements_abs.touch()
    if code:
        (project_cwd / code).mkdir(parents=True, exist_ok=True)

    # Act
    layer = Layer(
        layer_name, code=code, requirements=requirements, runtime=runtime, architecture=arch
    )

    # Assert
    def check_resources(_):
        layer_versions = pulumi_mocks.created_layer_versions(TP + layer_name)
        assert len(layer_versions) == 1
        layer_args = layer_versions[0]
        assert layer_args.inputs["layerName"] == TP + layer_name
        assert layer_args.inputs["compatibleRuntimes"] == [runtime or DEFAULT_RUNTIME]
        assert layer_args.inputs["compatibleArchitectures"] == [arch or DEFAULT_ARCHITECTURE]
        code_archive: AssetArchive = layer_args.inputs["code"]
        assert isinstance(code_archive, AssetArchive)
        assert len(code_archive.assets) == bool(code) + bool(requirements)

        # Check code archive
        if code:
            code_dir_name = Path(code).name
            expected_code_key = f"python/{code_dir_name}"
            assert expected_code_key in code_archive.assets
            code_archive_asset = code_archive.assets[expected_code_key]
            assert isinstance(code_archive_asset, FileArchive)
            assert code_archive_asset.path == str(project_cwd / code)

        if not requirements:
            mock_get_or_install_dependencies_layer.assert_not_called()
            return

        mock_get_or_install_dependencies_layer.assert_called_once_with(
            requirements_source=RequirementsSpec(
                content="\n".join(requirements) if isinstance(requirements, list) else None,
                path_from_root=Path(requirements) if isinstance(requirements, str) else None,
            ),
            runtime=runtime or DEFAULT_RUNTIME,
            architecture=arch or DEFAULT_ARCHITECTURE,
            project_root=project_cwd,
            log_context=f"Layer: {layer_name}",
            cache_subdirectory=_LAYER_CACHE_SUBDIR,
        )

        # Check dependencies archive
        expected_depencencies_key = f"python/lib/{runtime or DEFAULT_RUNTIME}/site-packages"
        assert expected_depencencies_key in code_archive.assets
        dependencies_archive_asset = code_archive.assets[expected_depencencies_key]
        assert isinstance(dependencies_archive_asset, FileArchive)
        assert dependencies_archive_asset.path == str(
            mock_get_or_install_dependencies_layer.return_value
        )

    layer.arn.apply(check_resources)


@pytest.mark.parametrize(
    ("opts", "error_type", "error_match"),
    [
        ({}, ValueError, "must specify 'code' and/or 'requirements'"),
        (
            {"requirements": [1, True]},
            TypeError,
            "If 'requirements' is a list, all its elements must be strings.",
        ),
        (
            {"requirements": True},
            TypeError,
            re.escape("'requirements' must be a string (path), list of strings, or None."),
        ),
        ({"requirements": "nonexistent.txt"}, FileNotFoundError, "Requirements file not found"),
        ({"requirements": "functions"}, ValueError, "Requirements path is not a file"),
        ({"code": "functions/simple.py"}, ValueError, "is not a directory"),
        ({"code": "src/non-existent-folder"}, ValueError, "is not a directory"),
        ({"code": "../outside-folder/"}, ValueError, "which is outside the project root"),
        (
            {"requirements": "../outside-folder/file.txt"},
            ValueError,
            "which is outside the project root",
        ),
    ],
    ids=[
        "no_code_or_requirements",
        "requirements_list_not_strings",
        "requirements_not_list_or_str_or_none",
        "requirements_path_does_not_exist",
        "requirements_path_is_folder",
        "code_path_is_not_a_folder",
        "code_path_does_not_exist",
        "code_path_outside_of_project_root",
        "requirements_path_outside_of_project_root",
    ],
)
def test_layer_raises_when__(project_cwd, opts, error_type, error_match):
    # Arrange
    outside_folder = project_cwd / "../outside-folder"
    outside_folder.mkdir(parents=True)
    outside_file = outside_folder / "file.txt"
    outside_file.touch()
    # Act & Assert
    with pytest.raises(error_type, match=error_match):
        _ = Layer(name="my-layer", **opts).resources


@pulumi.runtime.test
def test_layer_customize_layer_version_resource(
    pulumi_mocks, project_cwd, mock_cache_fs, mock_get_or_install_dependencies_layer
):
    """Test that customize parameter is applied to Lambda layer version resource."""
    # Arrange
    code_path = "src/my_layer_code"
    (project_cwd / code_path).mkdir(parents=True, exist_ok=True)

    layer = Layer(
        "custom-layer",
        code=code_path,
        customize={
            "layer_version": {
                "description": "Custom layer description",
            }
        },
    )

    # Act & Assert
    def check_resources(_):
        layer_versions = pulumi_mocks.created_layer_versions(TP + "custom-layer")
        assert len(layer_versions) == 1
        layer_args = layer_versions[0]

        # Check customization was applied
        assert layer_args.inputs.get("description") == "Custom layer description"

    layer.arn.apply(check_resources)
