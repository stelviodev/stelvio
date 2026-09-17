"""Fixtures shared by the Function test modules."""

from unittest.mock import patch

from pulumi import AssetArchive, StringAsset
from pytest import fixture

from stelvio.bridge.remote.infrastructure import AppSyncResource
from stelvio.context import AppContext, _ContextStore, context


@fixture
def dev_mode_context():
    """Run the test as `stlv dev` would: dev-mode app context, AppSync bridge discovery and
    the stub archive mocked. Yields the (discover, bridge archive) mocks."""
    ctx = context()
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(name=ctx.name, env=ctx.env, aws=ctx.aws, home="aws", dns=ctx.dns, dev_mode=True)
    )
    with (
        patch("stelvio.aws.function.function.discover_or_create_appsync") as mock_discover,
        patch("stelvio.aws.function.function._create_lambda_bridge_archive") as mock_archive,
    ):
        mock_discover.return_value = AppSyncResource(
            api_id="test-api-id",
            http_endpoint="https://test-http.appsync.amazonaws.com",
            realtime_endpoint="wss://test-realtime.appsync.amazonaws.com",
            api_key="test-api-key-123",
        )
        mock_archive.return_value = AssetArchive(
            {"stlv_function_stub.py": StringAsset("stub-content")}
        )
        yield mock_discover, mock_archive
