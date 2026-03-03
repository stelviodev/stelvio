"""AWS-specific test fixtures shared across aws test modules."""

import pytest
from pulumi.runtime import set_mocks

from stelvio.component import ComponentRegistry
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.provider import ProviderStore

from .pulumi_mocks import MockDns, PulumiTestMocks


@pytest.fixture
def pulumi_mocks():
    """Provide shared Pulumi mocks for AWS resource testing."""
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pytest.fixture
def mock_dns():
    """Provide a MockDns instance for tests that need DNS."""
    return MockDns()


@pytest.fixture
def app_context_with_dns(mock_dns):
    """App context with DNS provider configured in us-east-1."""
    _ContextStore.clear()
    ProviderStore.reset()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            dns=mock_dns,
        )
    )
    yield mock_dns
    _ContextStore.clear()


@pytest.fixture
def app_context_with_dns_eu_west(mock_dns):
    """App context with DNS provider configured in eu-west-1."""
    _ContextStore.clear()
    ProviderStore.reset()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="eu-west-1"),
            home="aws",
            dns=mock_dns,
        )
    )
    yield mock_dns
    _ContextStore.clear()


@pytest.fixture
def app_context_without_dns():
    """App context without DNS provider configured in us-east-1."""
    _ContextStore.clear()
    ProviderStore.reset()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            dns=None,
        )
    )
    yield
    _ContextStore.clear()


@pytest.fixture
def component_registry():
    """Provide a clean ComponentRegistry for tests."""
    ComponentRegistry._instances.clear()
    ComponentRegistry._registered_names.clear()
    yield ComponentRegistry
    ComponentRegistry._instances.clear()
    ComponentRegistry._registered_names.clear()
