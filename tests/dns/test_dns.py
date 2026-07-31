"""Behavioural tests for the Dns protocol opts contract."""

from dataclasses import replace

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.context import _ContextStore
from stelvio.dns import Record
from tests.aws.pulumi_mocks import PulumiTestMocks


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pulumi.runtime.test
def test_dns_adapter_without_opts_raises_type_error(pulumi_mocks):
    """Custom adapters that omit opts= must fail when Stelvio passes it."""

    class LegacyDns:
        def create_record(self, resource_name, name, record_type, value, ttl=1) -> Record:
            raise AssertionError("create_record should not be reached")

        def create_caa_record(self, resource_name, name, record_type, content, ttl=1) -> Record:
            raise AssertionError("create_caa_record should not succeed without opts")

    current_context = context()
    _ContextStore.clear()
    _ContextStore.set(replace(current_context, dns=LegacyDns()))

    acm = AcmValidatedDomain("break-cert", domain_name="api.example.com")
    with pytest.raises(TypeError, match="opts"):
        _ = acm.resources
