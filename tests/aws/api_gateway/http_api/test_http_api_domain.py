"""Tests for HttpApi custom-domain behavior."""

import pulumi
import pytest

from stelvio.aws.api_gateway.http_api import ApiDomain, HttpApi, HttpApiConfig
from stelvio.dns import DnsProviderNotConfiguredError

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid
from .conftest import TP, when_http_api_ready

pytestmark = pytest.mark.usefixtures("project_cwd")


@pulumi.runtime.test
def test_http_api_implicit_domain_creates_root_mapping_resource_graph(
    pulumi_mocks,
    app_context_with_dns,
):
    api = HttpApi("my-api", domain_name="api.example.com")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        certificate_arn = (
            f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/"
            f"{tid(TP + 'my-api-domain-cert-certificate')}"
        )
        pulumi_mocks.assert_res(
            "my-api-domain-cert-certificate",
            R.CERTIFICATE,
            {"domainName": "api.example.com", "validationMethod": "DNS"},
        )
        pulumi_mocks.assert_res(
            "my-api-domain-cert-certificate-validation",
            R.CERTIFICATE_VALIDATION,
            {
                "certificateArn": certificate_arn,
                "validationRecordFqdns": ["_test.api.example.com"],
            },
        )
        pulumi_mocks.assert_res(
            "my-api-domain-domain",
            R.HTTP_API_DOMAIN_NAME,
            {
                "domainName": "api.example.com",
                "domainNameConfiguration": {
                    "certificateArn": certificate_arn,
                    "endpointType": "REGIONAL",
                    "securityPolicy": "TLS_1_2",
                },
            },
        )
        pulumi_mocks.assert_res(
            "my-api-api-mapping",
            R.HTTP_API_MAPPING,
            {
                "apiId": tid(TP + "my-api")[:8],
                "domainName": "api.example.com",
                "stage": tid(TP + "my-api-stage"),
            },
        )
        pulumi_mocks.assert_res(
            "my-api-domain-cert-certificate-validation-record",
            R.CLOUDFLARE_RECORD,
            {
                "name": "_test.api.example.com",
                "type": "CNAME",
                "content": "test-validation.api.example.com",
                "ttl": 1.0,
            },
            partial=True,
        )
        pulumi_mocks.assert_res(
            "my-api-domain-dns-record",
            R.CLOUDFLARE_RECORD,
            {
                "name": "api.example.com",
                "type": "CNAME",
                "content": (
                    f"d-{tid(TP + 'my-api-domain-domain')}"
                    f".execute-api.{DEFAULT_REGION}.amazonaws.com"
                ),
                "ttl": 300.0,
            },
            partial=True,
        )
        pulumi_mocks.assert_res_counts(
            {
                R.API_ACCOUNT: 2,
                R.HTTP_API: 1,
                R.ROLE: 2,
                R.LOG_GROUP: 1,
                R.ROLE_POLICY_ATTACHMENT: 1,
                R.HTTP_API_STAGE: 1,
                R.FUNCTION: 1,
                R.HTTP_API_INTEGRATION: 1,
                R.LAMBDA_PERMISSION: 1,
                R.HTTP_API_ROUTE: 1,
                R.CERTIFICATE: 1,
                R.CLOUDFLARE_RECORD: 2,
                R.CERTIFICATE_VALIDATION: 1,
                R.HTTP_API_DOMAIN_NAME: 1,
                R.HTTP_API_MAPPING: 1,
            }
        )

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_config_accepts_domain_component(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain("shared-domain", domain_name="api.example.com")
    api = HttpApi(
        "my-api",
        config=HttpApiConfig(domain=domain, api_mapping_key="v1"),
    )
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        mappings = pulumi_mocks.created_http_api_mappings()
        assert len(mappings) == 1
        assert mappings[0].typ == "aws:apigatewayv2/apiMapping:ApiMapping"
        assert mappings[0].inputs["apiMappingKey"] == "v1"
        assert mappings[0].inputs["apiId"] == tid(TP + "my-api")[:8]
        assert mappings[0].inputs["stage"] == tid(TP + "my-api-stage")
        domains = pulumi_mocks.created_http_api_domain_names()
        assert len(domains) == 1
        assert mappings[0].inputs["domainName"] == domains[0].inputs["domainName"]

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_config_dict_accepts_domain_component(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain("shared-domain", domain_name="api.example.com")
    api = HttpApi("my-api", config={"domain": domain, "api_mapping_key": "v2"})
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        mappings = pulumi_mocks.created_http_api_mappings()
        assert len(mappings) == 1
        assert mappings[0].typ == "aws:apigatewayv2/apiMapping:ApiMapping"
        assert mappings[0].inputs["apiMappingKey"] == "v2"

    when_http_api_ready(api, check)


def test_http_api_config_conflicts_with_domain_option(app_context_with_dns):
    config_domain = ApiDomain("config-domain", domain_name="api.example.com")
    keyword_domain = ApiDomain("keyword-domain", domain_name="other.example.com")

    with pytest.raises(ValueError, match="cannot combine 'config' parameter"):
        HttpApi("my-api", config=HttpApiConfig(domain=config_domain), domain=keyword_domain)


@pulumi.runtime.test
def test_http_api_domain_dns_record_customize_applies_only_to_public_record(
    pulumi_mocks,
    app_context_with_dns,
):
    domain = ApiDomain(
        "shared-domain",
        domain_name="api.example.com",
        customize={"dns_record": {"ttl": 600}},
    )
    _ = domain.resources

    def check(_):
        records = app_context_with_dns.created_records
        assert len(records) == 2
        validation_record = records[0]
        public_record = records[1]
        assert validation_record[4] == 1
        assert public_record[1] == "api.example.com"
        assert public_record[2] == "CNAME"
        assert public_record[4] == 600

    domain.resources.custom_domain.domain_name.apply(check)


@pulumi.runtime.test
def test_http_api_domain_customize_domain_key(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain(
        "shared-domain",
        domain_name="api.example.com",
        customize={"domain": {"tags": {"Purpose": "test"}}},
    )
    _ = domain.resources

    def check(_):
        domains = pulumi_mocks.created_http_api_domain_names()
        assert len(domains) == 1
        assert domains[0].inputs["tags"] == {"Purpose": "test"}

    domain.resources.custom_domain.domain_name.apply(check)


def test_http_api_domain_requires_dns_provider(app_context_without_dns):
    domain = ApiDomain("shared-domain", domain_name="api.example.com")

    with pytest.raises(DnsProviderNotConfiguredError, match="DNS provider is not configured"):
        _ = domain.resources


def test_http_api_implicit_domain_name_collision(app_context_with_dns):
    ApiDomain("my-api-domain", domain_name="other.example.com")
    api = HttpApi("my-api", domain_name="api.example.com")
    api.route("GET", "/users", "functions/simple.handler")

    with pytest.raises(ValueError, match="Duplicate Stelvio component name"):
        _ = api.resources


@pulumi.runtime.test
def test_http_api_domain_duplicate_mapping_key_raises(pulumi_mocks, app_context_with_dns):
    """Two HttpApis sharing the same HttpApiDomain with the same mapping_key conflict."""
    domain = ApiDomain("shared", domain_name="api.example.com")
    api1 = HttpApi("api-one", domain=domain, api_mapping_key="v1")
    api1.route("GET", "/users", "functions/simple.handler")
    api2 = HttpApi("api-two", domain=domain, api_mapping_key="v1")
    api2.route("GET", "/orders", "functions/simple.handler")

    _ = api1.resources
    with pytest.raises(ValueError, match=r"Duplicate api_mapping_key"):
        _ = api2.resources


@pulumi.runtime.test
def test_http_api_domain_distinct_mapping_keys_allowed(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain("shared", domain_name="api.example.com")
    api1 = HttpApi("api-one", domain=domain, api_mapping_key="v1")
    api1.route("GET", "/users", "functions/simple.handler")
    api2 = HttpApi("api-two", domain=domain, api_mapping_key="v2")
    api2.route("GET", "/orders", "functions/simple.handler")

    _ = api1.resources
    _ = api2.resources

    def check(_):
        mappings = pulumi_mocks.created_http_api_mappings()
        mappings_by_key = {mapping.inputs["apiMappingKey"]: mapping for mapping in mappings}
        assert set(mappings_by_key) == {"v1", "v2"}
        assert mappings_by_key["v1"].inputs["apiId"] == tid(TP + "api-one")[:8]
        assert mappings_by_key["v2"].inputs["apiId"] == tid(TP + "api-two")[:8]

    when_http_api_ready(api2, check)
