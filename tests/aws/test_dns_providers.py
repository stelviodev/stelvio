"""Behavioural tests that built-in DNS providers forward ResourceOptions."""

from __future__ import annotations

from dataclasses import dataclass

import pulumi
from pytest import fixture, raises

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.dns import Route53Dns
from stelvio.cloudflare.dns import CloudflareDns
from stelvio.component import Component
from stelvio.provider import ProviderStore

from .conftest import assert_urn
from .pulumi_mocks import R


@dataclass(frozen=True)
class _DnsParentResources:
    pass


class _DnsParent(Component[_DnsParentResources, dict]):
    def __init__(self, name: str):
        super().__init__(ProviderStore.aws(), "stelvio:test:DnsParent", name)

    def _create_resources(self) -> _DnsParentResources:
        return _DnsParentResources()


class LegacyDns:
    def create_record(self, resource_name, name, record_type, value, ttl=1):
        raise AssertionError("create_record should not be reached")


@fixture
def mock_dns():
    return LegacyDns()


@pulumi.runtime.test
def test_route53_create_record_forwards_opts_parent(pulumi_mocks):
    parent = _DnsParent("route53-parent")
    dns = Route53Dns(zone_id="Z1234567890")
    record = dns.create_record(
        "route53-alias",
        name="api.example.com",
        record_type="CNAME",
        value="target.example.com",
        ttl=300,
        opts=parent._resource_opts(),
    )

    def check(urn: str):
        assert_urn(urn, "stelvio:test:DnsParent", R.ROUTE53_RECORD, "route53-alias")
        pulumi_mocks.assert_res(
            "route53-alias",
            R.ROUTE53_RECORD,
            {
                "name": "api.example.com",
                "type": "CNAME",
                "records": ["target.example.com"],
                "ttl": 300,
                "zoneId": "Z1234567890",
            },
            prefixed=False,
        )

    return record.pulumi_resource.urn.apply(check)


@pulumi.runtime.test
def test_cloudflare_create_record_forwards_opts_parent(pulumi_mocks):
    parent = _DnsParent("cf-parent")
    dns = CloudflareDns(zone_id="cf-zone-id")
    record = dns.create_record(
        "cf-alias",
        name="api.example.com",
        record_type="CNAME",
        value="target.example.com",
        ttl=300,
        opts=parent._resource_opts(),
    )

    def check(urn: str):
        assert_urn(urn, "stelvio:test:DnsParent", R.CLOUDFLARE_RECORD, "cf-alias")
        pulumi_mocks.assert_res(
            "cf-alias",
            R.CLOUDFLARE_RECORD,
            {
                "name": "api.example.com",
                "type": "CNAME",
                "content": "target.example.com",
                "ttl": 300.0,
                "zoneId": "cf-zone-id",
            },
            prefixed=False,
        )

    return record.pulumi_resource.urn.apply(check)


@pulumi.runtime.test
def test_dns_adapter_without_opts_raises_type_error(pulumi_mocks, app_context_with_dns):
    acm = AcmValidatedDomain("break-cert", domain_name="api.example.com")
    with raises(TypeError, match="opts"):
        _ = acm.resources
