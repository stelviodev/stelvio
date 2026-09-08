"""Behavioural tests that built-in DNS providers forward ResourceOptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pulumi
from pulumi import ResourceOptions

from stelvio.aws.dns import Route53Dns
from stelvio.cloudflare.dns import CloudflareDns
from stelvio.component import Component
from stelvio.provider import ProviderStore

from .pulumi_mocks import MockDns, R

if TYPE_CHECKING:
    from stelvio.dns import Record


@dataclass(frozen=True)
class _DnsParentResources:
    pass


class _DnsParent(Component[_DnsParentResources, dict]):
    def __init__(self, name: str):
        super().__init__(ProviderStore.aws(), "stelvio:test:DnsParent", name)

    def _create_resources(self) -> _DnsParentResources:
        return _DnsParentResources()


def _assert_parented(record: Record, parent_token: str):
    def check(urn: str):
        assert parent_token in urn

    return record.pulumi_resource.urn.apply(check)


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
        assert "::stelvio:test:DnsParent$" in urn
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
            partial=True,
            prefixed=False,
        )

    return record.pulumi_resource.urn.apply(check)


@pulumi.runtime.test
def test_route53_create_caa_record_forwards_opts_parent(pulumi_mocks):
    parent = _DnsParent("route53-caa-parent")
    dns = Route53Dns(zone_id="Z1234567890")
    record = dns.create_caa_record(
        "route53-validation",
        name="_test.api.example.com",
        record_type="CNAME",
        content="test-validation.api.example.com",
        opts=parent._resource_opts(),
    )
    return _assert_parented(record, "::stelvio:test:DnsParent$")


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
        assert "::stelvio:test:DnsParent$" in urn
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
            partial=True,
            prefixed=False,
        )

    return record.pulumi_resource.urn.apply(check)


@pulumi.runtime.test
def test_cloudflare_create_caa_record_forwards_opts_parent(pulumi_mocks):
    parent = _DnsParent("cf-caa-parent")
    dns = CloudflareDns(zone_id="cf-zone-id")
    record = dns.create_caa_record(
        "cf-validation",
        name="_test.api.example.com",
        record_type="CNAME",
        content="test-validation.api.example.com",
        opts=parent._resource_opts(),
    )
    return _assert_parented(record, "::stelvio:test:DnsParent$")


@pulumi.runtime.test
def test_mock_dns_create_record_forwards_opts_parent(pulumi_mocks):
    parent = _DnsParent("mock-parent")
    dns = MockDns()
    record = dns.create_record(
        "mock-alias",
        name="api.example.com",
        record_type="CNAME",
        value="target.example.com",
        opts=ResourceOptions(parent=parent),
    )
    return _assert_parented(record, "::stelvio:test:DnsParent$")
