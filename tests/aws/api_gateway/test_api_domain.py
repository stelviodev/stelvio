"""Tests for ApiDomain on its own (shared by HttpApi and WebsocketApi)."""

from __future__ import annotations

import pulumi
from pytest import raises

from stelvio.aws.api_gateway import ApiDomain

from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, TP, R, tid
from .conftest import API_DOMAIN_GRAPH_COUNTS, assert_api_domain_graph

_EXTERNAL_CERT_ARN = f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/pre-provisioned"


def test_api_domain_dns_record_customize_applies_only_to_public_record(
    pulumi_mocks,
    app_context_with_dns,
):
    domain = ApiDomain(
        "shared-domain",
        domain_name="api.example.com",
        customize={"dns_record": {"ttl": 600}},
    )

    @pulumi.runtime.test
    def deploy():
        return domain.resources

    deploy()

    assert_api_domain_graph(
        pulumi_mocks,
        domain_component="shared-domain",
        domain_name="api.example.com",
        dns_record_extra_inputs={"ttl": 600.0},
    )
    # Public record ttl customized; validation record remains ttl 1.
    validation = app_context_with_dns.created_records[0]
    public = app_context_with_dns.created_records[1]
    assert validation[4] == 1
    assert public[4] == 600
    pulumi_mocks.assert_res_counts(dict(API_DOMAIN_GRAPH_COUNTS))


def test_api_domain_customize_domain_key(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain(
        "shared-domain",
        domain_name="api.example.com",
        customize={"domain": {"tags": {"Purpose": "test"}}},
    )

    @pulumi.runtime.test
    def deploy():
        return domain.resources

    deploy()

    assert_api_domain_graph(
        pulumi_mocks,
        domain_component="shared-domain",
        domain_name="api.example.com",
        domain_extra_inputs={"tags": {"Purpose": "test"}},
    )
    pulumi_mocks.assert_res_counts(dict(API_DOMAIN_GRAPH_COUNTS))


def test_api_domain_uses_external_certificate_arn(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain(
        "shared-domain",
        domain_name="api.example.com",
        certificate_arn=_EXTERNAL_CERT_ARN,
    )

    @pulumi.runtime.test
    def deploy():
        return domain.resources

    deploy()

    assert domain.resources.acm_domain is None
    pulumi_mocks.assert_res(
        "shared-domain-domain",
        R.HTTP_API_DOMAIN_NAME,
        {
            "domainName": "api.example.com",
            "domainNameConfiguration": {
                "certificateArn": _EXTERNAL_CERT_ARN,
                "endpointType": "REGIONAL",
                "securityPolicy": "TLS_1_2",
            },
        },
    )
    pulumi_mocks.assert_res(
        "shared-domain-dns-record",
        R.CLOUDFLARE_RECORD,
        {
            "name": "api.example.com",
            "type": "CNAME",
            "content": (
                f"d-{tid(TP + 'shared-domain-domain')}.execute-api.{DEFAULT_REGION}.amazonaws.com"
            ),
            "ttl": 300.0,
            "zoneId": "test-zone-id",
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.HTTP_API_DOMAIN_NAME: 1,
            R.CLOUDFLARE_RECORD: 1,
        }
    )


def test_api_domain_rejects_certificate_arn_with_certificate_customize(
    app_context_with_dns,
):
    with raises(ValueError, match="Cannot specify both 'certificate_arn'"):
        ApiDomain(
            "shared-domain",
            domain_name="api.example.com",
            certificate_arn=_EXTERNAL_CERT_ARN,
            customize={"certificate": {"tags": {"Env": "test"}}},
        )
