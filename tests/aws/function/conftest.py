"""Fixtures shared by the Function test modules."""

from unittest.mock import patch

import pytest
from pulumi import AssetArchive, StringAsset

from stelvio.bridge.remote.infrastructure import AppSyncResource
from stelvio.context import AppContext, _ContextStore, context


@pytest.fixture
def dev_mode_context():
    """Switch the app context to dev mode and mock out the AppSync bridge.

    Yields the `discover_or_create_appsync` and `_create_lambda_bridge_archive` mocks.
    """
    ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name=ctx.name,
            env=ctx.env,
            aws=ctx.aws,
            home="aws",
            dns=ctx.dns,
            dev_mode=True,
        )
    )
    try:
        with (
            patch("stelvio.aws.function.function.discover_or_create_appsync") as mock_discover,
            patch(
                "stelvio.aws.function.function._create_lambda_bridge_archive"
            ) as mock_bridge_archive,
        ):
            mock_discover.return_value = AppSyncResource(
                api_id="test-api-id",
                http_endpoint="https://test-http.appsync.amazonaws.com",
                realtime_endpoint="wss://test-realtime.appsync.amazonaws.com",
                api_key="test-api-key-123",
            )
            mock_bridge_archive.return_value = AssetArchive(
                {"stlv_function_stub.py": StringAsset("stub-content")}
            )
            yield mock_discover, mock_bridge_archive
    finally:
        _ContextStore.clear()
        _ContextStore.set(ctx)
