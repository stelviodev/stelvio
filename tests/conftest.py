import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from stelvio.aws.function import FunctionAssetsRegistry, LinkPropertiesRegistry
from stelvio.component import ComponentRegistry


@pytest.fixture(autouse=True)
def clean_registries():
    LinkPropertiesRegistry._folder_links_properties_map.clear()
    ComponentRegistry._instances.clear()
    ComponentRegistry._registered_names.clear()
    ComponentRegistry._user_link_creators.clear()
    FunctionAssetsRegistry._functions_assets_map.clear()


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
