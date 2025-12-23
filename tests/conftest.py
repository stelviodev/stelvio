import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from stelvio.aws.function.function import LinkPropertiesRegistry
from stelvio.component import ComponentRegistry
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore


@pytest.fixture(autouse=True)
def clean_registries():
    LinkPropertiesRegistry._folder_links_properties_map.clear()
    ComponentRegistry._instances.clear()
    ComponentRegistry._registered_names.clear()
    ComponentRegistry._user_link_creators.clear()


def mock_get_or_install_dependencies(path: str):
    with patch(path) as mock_ensure:
        # Simulate get_or_install_dependencies returning a valid cache path
        # Use a unique path per test potentially, or ensure cleanup
        mock_cache_path = Path("mock_cache_dir_for_fixture").resolve()
        mock_cache_path.mkdir(parents=True, exist_ok=True)
        # Add a dummy file to simulate non-empty cache after install
        (mock_cache_path / "dummy_installed_package").touch()
        mock_ensure.return_value = mock_cache_path
        yield mock_ensure
        # Clean up the dummy cache dir after test
        shutil.rmtree(mock_cache_path, ignore_errors=True)


@pytest.fixture
def mock_get_or_install_dependencies_layer():
    yield from mock_get_or_install_dependencies("stelvio.aws.layer.get_or_install_dependencies")


@pytest.fixture
def mock_get_or_install_dependencies_function():
    yield from mock_get_or_install_dependencies(
        "stelvio.aws.function.dependencies.get_or_install_dependencies"
    )


@pytest.fixture(autouse=True)
def app_context():
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
        )
    )


@pytest.fixture
def project_cwd(monkeypatch, pytestconfig, tmp_path):
    """Provide a temporary Stelvio project root and chdir into it.

    This matches what Function-related tests need and can be reused
    by other components (e.g. CloudFront router) that instantiate
    real Functions with file-based handlers.
    """
    from stelvio.project import get_project_root

    # Ensure no stale cache from previous tests
    get_project_root.cache_clear()

    # Use the existing sample project as a realistic template
    rootpath = pytestconfig.rootpath
    source_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    temp_project_dir = tmp_path / "sample_project_copy"

    shutil.copytree(source_project_dir, temp_project_dir, dirs_exist_ok=True)

    # Remember original cwd and switch into the temp project
    original_cwd = Path.cwd()
    monkeypatch.chdir(temp_project_dir)

    yield temp_project_dir

    # Restore state after test
    monkeypatch.chdir(original_cwd)
    get_project_root.cache_clear()
