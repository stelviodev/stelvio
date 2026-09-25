"""AWS-specific test fixtures shared across aws test modules."""

import re

import pytest

from stelvio.aws.api_gateway.iam import _create_api_gateway_account_and_role
from stelvio.aws.document_db import _document_db_ca_path
from stelvio.component import ComponentRegistry
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.provider import ProviderStore

from .pulumi_mocks import TP, MockDns

FAKE_DOCDB_CA_PEM = b"-----BEGIN CERTIFICATE-----\nMIIBfake\n-----END CERTIFICATE-----\n"


@pytest.fixture(autouse=True)
def reset_api_gateway_cache():
    _create_api_gateway_account_and_role.cache_clear()
    yield
    _create_api_gateway_account_and_role.cache_clear()


class FakeUrlopenResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._data


@pytest.fixture(autouse=True)
def mock_docdb_ca_urlopen(monkeypatch):
    """Keep DocumentDb links off the network; record the CA bundle URLs requested."""
    calls: list[str] = []

    def fake_urlopen(url: str, **_kwargs: object) -> FakeUrlopenResponse:
        calls.append(url)
        return FakeUrlopenResponse(FAKE_DOCDB_CA_PEM)

    monkeypatch.setattr("stelvio.aws.document_db.urlopen", fake_urlopen)
    _document_db_ca_path.cache_clear()
    yield calls
    _document_db_ca_path.cache_clear()


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


def assert_urn(urn: str, parent_type: str, child_type: str, name: str) -> None:
    assert urn == f"urn:pulumi:stack::project::{parent_type}${child_type}::{name}"


def assert_hash_truncated(name: str, length: int) -> None:
    """A name that overflowed its limit: prefixed, cut to exactly `length`, 7-hex hash tail."""
    assert len(name) == length
    assert name.startswith(TP)
    assert re.fullmatch(r".+-[0-9a-f]{7}", name)


def spy_old_names(monkeypatch, component_cls) -> list[str]:
    """Collect the ``old_name`` each ``_resource_opts`` call passes on ``component_cls``.

    The mocks never see aliases, so the seam is the call that adds them.
    """
    seen: list[str] = []
    original = component_cls._resource_opts

    def spy(self, **kwargs):
        if kwargs.get("old_name"):
            seen.append(kwargs["old_name"])
        return original(self, **kwargs)

    monkeypatch.setattr(component_cls, "_resource_opts", spy)
    return seen
