"""Tests for HttpApi custom-domain behavior."""

import pulumi
import pytest

from stelvio.aws.api_gateway.http_api import HttpApi, HttpApiConfig, HttpApiDomain

from .conftest import when_http_api_ready

pytestmark = pytest.mark.usefixtures("project_cwd")


@pulumi.runtime.test
def test_http_api_config_accepts_domain_component(pulumi_mocks, app_context_with_dns):
    domain = HttpApiDomain("shared-domain", domain_name="api.example.com")
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

    when_http_api_ready(api, check)


@pulumi.runtime.test
def test_http_api_config_dict_accepts_domain_component(pulumi_mocks, app_context_with_dns):
    domain = HttpApiDomain("shared-domain", domain_name="api.example.com")
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
    config_domain = HttpApiDomain("config-domain", domain_name="api.example.com")
    keyword_domain = HttpApiDomain("keyword-domain", domain_name="other.example.com")

    with pytest.raises(ValueError, match="cannot combine 'config' parameter"):
        HttpApi("my-api", config=HttpApiConfig(domain=config_domain), domain=keyword_domain)


@pulumi.runtime.test
def test_http_api_domain_dns_record_customize_applies_only_to_public_record(
    app_context_with_dns,
):
    domain = HttpApiDomain(
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
def test_http_api_domain_customize_domain_key(app_context_with_dns):
    domain = HttpApiDomain(
        "shared-domain",
        domain_name="api.example.com",
        customize={"domain": {"tags": {"Purpose": "test"}}},
    )
    _ = domain.resources

    def check(_):
        domains = [r for r in app_context_with_dns.created_records if r[1] == "api.example.com"]
        assert domains

    domain.resources.custom_domain.domain_name.apply(check)


def test_http_api_implicit_domain_name_collision(app_context_with_dns):
    HttpApiDomain("my-api-domain", domain_name="other.example.com")
    api = HttpApi("my-api", domain_name="api.example.com")
    api.route("GET", "/users", "functions/simple.handler")

    with pytest.raises(ValueError, match="Duplicate Stelvio component name"):
        _ = api.resources
