from unittest.mock import MagicMock, patch

import pytest
from pulumi import AssetArchive, StringAsset

from stelvio.bridge.remote.infrastructure import (
    AppSyncResource,
    _create_lambda_bridge_archive,
    create_appsync_api,
    discover_or_create_appsync,
    find_or_create_appsync_api,
)


def test_create_lambda_bridge_archive_success(tmp_path):
    """Test successful creation of Lambda bridge archive."""
    # Create temporary directory structure
    stub_dir = tmp_path / "stelvio" / "bridge" / "remote" / "stub"
    stub_dir.mkdir(parents=True)
    stub_file = stub_dir / "function_stub.py"
    stub_content = "# Mock stub function code\nprint('Hello')"
    stub_file.write_text(stub_content)

    mock_cache_dir = tmp_path / "cache"
    mock_cache_dir.mkdir()

    with (
        patch("stelvio.bridge.remote.infrastructure.get_stelvio_lib_root") as mock_get_lib,
        patch("stelvio.bridge.remote.infrastructure.get_project_root") as mock_get_proj,
        patch("stelvio.bridge.remote.infrastructure.get_or_install_dependencies") as mock_get_deps,
    ):
        # Setup mocks
        mock_get_lib.return_value = tmp_path / "stelvio"
        mock_get_proj.return_value = tmp_path / "project"
        mock_get_deps.return_value = mock_cache_dir

        result = _create_lambda_bridge_archive()

        # Verify result is an AssetArchive
        assert isinstance(result, AssetArchive)

        # Verify get_or_install_dependencies was called with correct parameters
        mock_get_deps.assert_called_once()
        call_args = mock_get_deps.call_args
        assert call_args[1]["runtime"] == "python3.12"
        assert call_args[1]["architecture"] == "x86_64"
        assert call_args[1]["cache_subdirectory"] == "bridge_stub"
        assert call_args[1]["log_context"] == "Bridge Stub"


def test_create_lambda_bridge_archive_path_not_found(tmp_path):
    """Test that RuntimeError is raised when bridge path doesn't exist."""
    # Create a directory without the stub subdirectory
    stub_dir = tmp_path / "stelvio" / "bridge" / "remote"
    stub_dir.mkdir(parents=True)
    # Don't create the "stub" directory

    with patch("stelvio.bridge.remote.infrastructure.get_stelvio_lib_root") as mock_get_lib:
        mock_get_lib.return_value = tmp_path / "stelvio"

        with pytest.raises(RuntimeError, match="Could not create Stelvio Tunnel Lambda archive"):
            _create_lambda_bridge_archive()


def test_discover_or_create_appsync_with_profile():
    """Test discover_or_create_appsync with a specific profile."""
    mock_client = MagicMock()
    mock_resource = AppSyncResource(
        api_id="test-api-id",
        http_endpoint="https://test.appsync-api.us-east-1.amazonaws.com",
        realtime_endpoint="https://test.appsync-realtime-api.us-east-1.amazonaws.com",
        api_key="test-api-key",
    )

    with (
        patch("stelvio.bridge.remote.infrastructure.boto3.Session") as mock_session,
        patch(
            "stelvio.bridge.remote.infrastructure.find_or_create_appsync_api"
        ) as mock_find_create,
    ):
        # Setup mocks
        mock_session_instance = MagicMock()
        mock_session.return_value = mock_session_instance
        mock_session_instance.client.return_value = mock_client
        mock_find_create.return_value = mock_resource

        result = discover_or_create_appsync(region="us-west-2", profile="my-profile")

        # Assertions
        mock_session.assert_called_once_with(profile_name="my-profile", region_name="us-west-2")
        mock_session_instance.client.assert_called_once_with("appsync")
        mock_find_create.assert_called_once_with(mock_client)
        assert result == mock_resource


def test_discover_or_create_appsync_default_region():
    """Test discover_or_create_appsync with default region."""
    mock_client = MagicMock()
    mock_resource = AppSyncResource(
        api_id="test-api-id",
        http_endpoint="https://test.appsync-api.us-east-1.amazonaws.com",
        realtime_endpoint="https://test.appsync-realtime-api.us-east-1.amazonaws.com",
        api_key="test-api-key",
    )

    with (
        patch("stelvio.bridge.remote.infrastructure.boto3.Session") as mock_session,
        patch(
            "stelvio.bridge.remote.infrastructure.find_or_create_appsync_api"
        ) as mock_find_create,
    ):
        mock_session_instance = MagicMock()
        mock_session.return_value = mock_session_instance
        mock_session_instance.client.return_value = mock_client
        mock_find_create.return_value = mock_resource

        result = discover_or_create_appsync()

        mock_session.assert_called_once_with(profile_name=None, region_name="us-east-1")
        assert result == mock_resource


def test_find_or_create_appsync_api_found():
    """Test finding an existing AppSync API."""
    mock_client = MagicMock()

    # Mock paginator that returns existing API
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {
            "apis": [
                {"name": "other-api", "apiId": "other-id"},
                {"name": "stelvio", "apiId": "existing-api-id"},
            ]
        }
    ]

    # Mock get_api response
    mock_client.get_api.return_value = {
        "api": {
            "apiId": "existing-api-id",
            "name": "stelvio",
            "dns": {
                "HTTP": "https://existing.appsync-api.us-east-1.amazonaws.com",
                "REALTIME": "https://existing.appsync-realtime-api.us-east-1.amazonaws.com",
            },
        }
    }

    # Mock list_api_keys response
    mock_client.list_api_keys.return_value = {
        "apiKeys": [{"id": "existing-api-key", "expires": 1234567890}]
    }

    result = find_or_create_appsync_api(mock_client)

    # Assertions
    assert result.api_id == "existing-api-id"
    assert result.http_endpoint == "https://existing.appsync-api.us-east-1.amazonaws.com"
    assert (
        result.realtime_endpoint == "https://existing.appsync-realtime-api.us-east-1.amazonaws.com"
    )
    assert result.api_key == "existing-api-key"

    mock_client.get_api.assert_called_once_with(apiId="existing-api-id")
    mock_client.list_api_keys.assert_called_once_with(apiId="existing-api-id")
    # Should NOT call create_api
    mock_client.create_api.assert_not_called()


def test_find_or_create_appsync_api_not_found():
    """Test creating a new AppSync API when not found."""
    mock_client = MagicMock()

    # Mock paginator that returns no matching API
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [{"apis": [{"name": "other-api", "apiId": "other-id"}]}]

    # Mock create_appsync_api behavior
    with patch("stelvio.bridge.remote.infrastructure.create_appsync_api") as mock_create_appsync:
        mock_resource = AppSyncResource(
            api_id="new-api-id",
            http_endpoint="https://new.appsync-api.us-east-1.amazonaws.com",
            realtime_endpoint="https://new.appsync-realtime-api.us-east-1.amazonaws.com",
            api_key="new-api-key",
        )
        mock_create_appsync.return_value = mock_resource

        result = find_or_create_appsync_api(mock_client)

        # Should have called create_appsync_api
        mock_create_appsync.assert_called_once_with(mock_client, "stelvio")
        assert result == mock_resource


def test_find_or_create_appsync_api_found_no_keys():
    """Test finding an existing AppSync API with no API keys."""
    mock_client = MagicMock()

    # Mock paginator
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [{"apis": [{"name": "stelvio", "apiId": "test-id"}]}]

    # Mock get_api response
    mock_client.get_api.return_value = {
        "api": {
            "apiId": "test-id",
            "name": "stelvio",
            "dns": {
                "HTTP": "https://test.appsync-api.us-east-1.amazonaws.com",
                "REALTIME": "https://test.appsync-realtime-api.us-east-1.amazonaws.com",
            },
        }
    }

    # Mock list_api_keys with no keys
    mock_client.list_api_keys.return_value = {"apiKeys": []}

    result = find_or_create_appsync_api(mock_client)

    assert result.api_id == "test-id"
    assert result.api_key is None


def test_create_appsync_api_success():
    """Test successful creation of a new AppSync API."""
    mock_client = MagicMock()

    # Mock create_api response
    mock_client.create_api.return_value = {
        "api": {
            "apiId": "new-api-id",
            "name": "stelvio",
            "dns": {
                "HTTP": "https://new.appsync-api.us-east-1.amazonaws.com",
                "REALTIME": "https://new.appsync-realtime-api.us-east-1.amazonaws.com",
            },
        }
    }

    # Mock create_channel_namespace response
    mock_client.create_channel_namespace.return_value = {}

    # Mock create_api_key response
    mock_client.create_api_key.return_value = {
        "apiKey": {"id": "new-api-key", "expires": 1234567890}
    }

    with patch("stelvio.bridge.remote.infrastructure.time.time") as mock_time:
        mock_time.return_value = 1000000000

        result = create_appsync_api(mock_client, "stelvio")

        # Verify create_api was called with correct config
        mock_client.create_api.assert_called_once()
        create_api_args = mock_client.create_api.call_args[1]
        assert create_api_args["name"] == "stelvio"
        assert "eventConfig" in create_api_args
        assert create_api_args["eventConfig"]["authProviders"] == [
            {"authType": "API_KEY"},
            {"authType": "AWS_IAM"},
        ]

        # Verify create_channel_namespace was called
        mock_client.create_channel_namespace.assert_called_once_with(
            apiId="new-api-id",
            name="stelvio",
            subscribeAuthModes=[{"authType": "API_KEY"}],
            publishAuthModes=[{"authType": "API_KEY"}],
        )

        # Verify create_api_key was called with correct expiration
        mock_client.create_api_key.assert_called_once_with(
            apiId="new-api-id",
            expires=1000000000 + (365 * 24 * 60 * 60),  # 1 year
        )

        # Verify returned resource
        assert result.api_id == "new-api-id"
        assert result.http_endpoint == "https://new.appsync-api.us-east-1.amazonaws.com"
        assert (
            result.realtime_endpoint == "https://new.appsync-realtime-api.us-east-1.amazonaws.com"
        )
        assert result.api_key == "new-api-key"


def test_appsync_resource_dataclass():
    """Test AppSyncResource dataclass is frozen and immutable."""
    resource = AppSyncResource(
        api_id="test-id",
        http_endpoint="https://test.com",
        realtime_endpoint="wss://test.com",
        api_key="test-key",
    )

    # Verify attributes
    assert resource.api_id == "test-id"
    assert resource.http_endpoint == "https://test.com"
    assert resource.realtime_endpoint == "wss://test.com"
    assert resource.api_key == "test-key"

    # Verify it's frozen (immutable)
    with pytest.raises(AttributeError):
        resource.api_id = "new-id"


def test_find_or_create_appsync_api_multiple_pages():
    """Test finding API across multiple paginated results."""
    mock_client = MagicMock()

    # Mock paginator with multiple pages
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"apis": [{"name": "api1", "apiId": "id1"}]},
        {"apis": [{"name": "api2", "apiId": "id2"}]},
        {"apis": [{"name": "stelvio", "apiId": "found-id"}]},
    ]

    # Mock get_api response
    mock_client.get_api.return_value = {
        "api": {
            "apiId": "found-id",
            "name": "stelvio",
            "dns": {
                "HTTP": "https://found.appsync-api.us-east-1.amazonaws.com",
                "REALTIME": "https://found.appsync-realtime-api.us-east-1.amazonaws.com",
            },
        }
    }

    # Mock list_api_keys response
    mock_client.list_api_keys.return_value = {"apiKeys": [{"id": "found-key"}]}

    result = find_or_create_appsync_api(mock_client)

    assert result.api_id == "found-id"
    assert result.api_key == "found-key"
    mock_client.get_api.assert_called_once_with(apiId="found-id")


def test_find_or_create_appsync_api_empty_pages():
    """Test handling empty pages during pagination."""
    mock_client = MagicMock()

    # Mock paginator with empty pages
    mock_paginator = MagicMock()
    mock_client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [{"apis": []}, {"apis": []}]

    with patch("stelvio.bridge.remote.infrastructure.create_appsync_api") as mock_create_appsync:
        mock_resource = AppSyncResource(
            api_id="new-api-id",
            http_endpoint="https://new.appsync-api.us-east-1.amazonaws.com",
            realtime_endpoint="https://new.appsync-realtime-api.us-east-1.amazonaws.com",
            api_key="new-api-key",
        )
        mock_create_appsync.return_value = mock_resource

        result = find_or_create_appsync_api(mock_client)

        mock_create_appsync.assert_called_once_with(mock_client, "stelvio")
        assert result == mock_resource


def test_create_appsync_api_with_multiple_api_keys():
    """Test create_appsync_api when multiple API keys exist (uses first one)."""
    mock_client = MagicMock()

    # Mock create_api response
    mock_client.create_api.return_value = {
        "api": {
            "apiId": "new-api-id",
            "name": "stelvio",
            "dns": {
                "HTTP": "https://new.appsync-api.us-east-1.amazonaws.com",
                "REALTIME": "https://new.appsync-realtime-api.us-east-1.amazonaws.com",
            },
        }
    }

    # Mock create_channel_namespace response
    mock_client.create_channel_namespace.return_value = {}

    # Mock create_api_key response
    mock_client.create_api_key.return_value = {"apiKey": {"id": "first-key"}}

    result = create_appsync_api(mock_client, "stelvio")

    assert result.api_id == "new-api-id"
    assert result.api_key == "first-key"


def test_create_lambda_bridge_archive_reads_file_content(tmp_path):
    """Test that the archive contains the actual stub file content."""
    # Create temporary directory structure
    stub_dir = tmp_path / "stelvio" / "bridge" / "remote" / "stub"
    stub_dir.mkdir(parents=True)
    stub_file = stub_dir / "function_stub.py"
    expected_content = "# This is the stub Lambda handler\ndef handler(event, context):\n    pass"
    stub_file.write_text(expected_content)

    mock_cache_dir = tmp_path / "cache"
    mock_cache_dir.mkdir()
    # Create a dummy file in cache to simulate installed dependencies
    (mock_cache_dir / "websockets").mkdir()

    with (
        patch("stelvio.bridge.remote.infrastructure.get_stelvio_lib_root") as mock_get_lib,
        patch("stelvio.bridge.remote.infrastructure.get_project_root") as mock_get_proj,
        patch("stelvio.bridge.remote.infrastructure.get_or_install_dependencies") as mock_get_deps,
    ):
        mock_get_lib.return_value = tmp_path / "stelvio"
        mock_get_proj.return_value = tmp_path / "project"
        mock_get_deps.return_value = mock_cache_dir

        result = _create_lambda_bridge_archive()

        assert isinstance(result, AssetArchive)
        # Verify the StringAsset contains the expected content
        assets_dict = result.assets
        assert "function_stub.py" in assets_dict
        stub_asset = assets_dict["function_stub.py"]
        assert isinstance(stub_asset, StringAsset)


def test_discover_or_create_appsync_no_profile():
    """Test discover_or_create_appsync with None profile."""
    mock_client = MagicMock()
    mock_resource = AppSyncResource(
        api_id="test-api-id",
        http_endpoint="https://test.appsync-api.us-east-1.amazonaws.com",
        realtime_endpoint="https://test.appsync-realtime-api.us-east-1.amazonaws.com",
        api_key="test-api-key",
    )

    with (
        patch("stelvio.bridge.remote.infrastructure.boto3.Session") as mock_session,
        patch(
            "stelvio.bridge.remote.infrastructure.find_or_create_appsync_api"
        ) as mock_find_create,
    ):
        mock_session_instance = MagicMock()
        mock_session.return_value = mock_session_instance
        mock_session_instance.client.return_value = mock_client
        mock_find_create.return_value = mock_resource

        result = discover_or_create_appsync(region="eu-west-1", profile=None)

        mock_session.assert_called_once_with(profile_name=None, region_name="eu-west-1")
        mock_session_instance.client.assert_called_once_with("appsync")
        assert result == mock_resource


def test_appsync_resource_equality():
    """Test AppSyncResource equality comparison."""
    resource1 = AppSyncResource(
        api_id="test-id",
        http_endpoint="https://test.com",
        realtime_endpoint="wss://test.com",
        api_key="test-key",
    )
    resource2 = AppSyncResource(
        api_id="test-id",
        http_endpoint="https://test.com",
        realtime_endpoint="wss://test.com",
        api_key="test-key",
    )
    resource3 = AppSyncResource(
        api_id="different-id",
        http_endpoint="https://test.com",
        realtime_endpoint="wss://test.com",
        api_key="test-key",
    )

    assert resource1 == resource2
    assert resource1 != resource3
