"""Tests for HttpApi custom-domain behavior."""

from __future__ import annotations

from typing import Any

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway.http_api import ApiDomain, HttpApi, HttpApiConfig
from stelvio.dns import DnsProviderNotConfiguredError

from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid
from .conftest import TP, when_http_api_ready

pytestmark = mark.usefixtures("project_cwd")

_DOMAIN_GRAPH_COUNTS = {
    R.CERTIFICATE: 1,
    R.CLOUDFLARE_RECORD: 2,
    R.CERTIFICATE_VALIDATION: 1,
    R.HTTP_API_DOMAIN_NAME: 1,
}


def _certificate_arn(domain_component: str) -> str:
    return (
        f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/"
        f"{tid(TP + f'{domain_component}-cert-certificate')}"
    )


def assert_api_domain_graph(
    mocks,
    *,
    domain_component: str,
    domain_name: str,
    domain_extra_inputs: dict[str, Any] | None = None,
    dns_record_extra_inputs: dict[str, Any] | None = None,
) -> None:
    """Assert ACM, DomainName, and DNS resources for an ApiDomain."""
    certificate_arn = _certificate_arn(domain_component)
    mocks.assert_res(
        f"{domain_component}-cert-certificate",
        R.CERTIFICATE,
        {"domainName": domain_name, "validationMethod": "DNS"},
    )
    mocks.assert_res(
        f"{domain_component}-cert-certificate-validation",
        R.CERTIFICATE_VALIDATION,
        {
            "certificateArn": certificate_arn,
            "validationRecordFqdns": [f"_test.{domain_name}"],
        },
    )
    domain_inputs: dict[str, Any] = {
        "domainName": domain_name,
        "domainNameConfiguration": {
            "certificateArn": certificate_arn,
            "endpointType": "REGIONAL",
            "securityPolicy": "TLS_1_2",
        },
    }
    if domain_extra_inputs:
        domain_inputs.update(domain_extra_inputs)
    mocks.assert_res(
        f"{domain_component}-domain",
        R.HTTP_API_DOMAIN_NAME,
        domain_inputs,
        partial=domain_extra_inputs is not None,
    )
    mocks.assert_res(
        f"{domain_component}-cert-certificate-validation-record",
        R.CLOUDFLARE_RECORD,
        {
            "name": f"_test.{domain_name}",
            "type": "CNAME",
            "content": f"test-validation.{domain_name}",
            "ttl": 1.0,
        },
        partial=True,
    )
    dns_inputs: dict[str, Any] = {
        "name": domain_name,
        "type": "CNAME",
        "content": (
            f"d-{tid(TP + f'{domain_component}-domain')}"
            f".execute-api.{DEFAULT_REGION}.amazonaws.com"
        ),
        "ttl": 300.0,
    }
    if dns_record_extra_inputs:
        dns_inputs.update(dns_record_extra_inputs)
    mocks.assert_res(
        f"{domain_component}-dns-record",
        R.CLOUDFLARE_RECORD,
        dns_inputs,
        partial=True,
    )


def assert_api_mapping(
    mocks,
    *,
    api_name: str,
    domain_name: str,
    mapping_key: str | None = None,
) -> None:
    """Assert an HttpApi ApiMapping with exact inputs (omit key for root mapping)."""
    inputs: dict[str, Any] = {
        "apiId": tid(TP + api_name)[:8],
        "domainName": domain_name,
        "stage": tid(TP + f"{api_name}-stage"),
    }
    if mapping_key is not None:
        inputs["apiMappingKey"] = mapping_key
    mocks.assert_res(f"{api_name}-api-mapping", R.HTTP_API_MAPPING, inputs)


def _http_api_with_domain_counts(
    *,
    api_count: int = 1,
    function_count: int = 1,
    mapping_count: int = 1,
) -> dict[R, int]:
    return {
        R.API_ACCOUNT: 2,
        R.HTTP_API: api_count,
        R.ROLE: function_count + 1,
        R.LOG_GROUP: api_count,
        R.ROLE_POLICY_ATTACHMENT: function_count,
        R.HTTP_API_STAGE: api_count,
        R.FUNCTION: function_count,
        R.HTTP_API_INTEGRATION: function_count,
        R.LAMBDA_PERMISSION: function_count,
        R.HTTP_API_ROUTE: function_count,
        R.HTTP_API_MAPPING: mapping_count,
        **_DOMAIN_GRAPH_COUNTS,
    }


@pulumi.runtime.test
def test_http_api_implicit_domain_creates_root_mapping_resource_graph(
    pulumi_mocks,
    app_context_with_dns,
):
    api = HttpApi("my-api", domain_name="api.example.com")
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        assert_api_domain_graph(
            pulumi_mocks,
            domain_component="my-api-domain",
            domain_name="api.example.com",
        )
        assert_api_mapping(
            pulumi_mocks,
            api_name="my-api",
            domain_name="api.example.com",
        )
        pulumi_mocks.assert_res_counts(_http_api_with_domain_counts())

    when_http_api_ready(api, check)


@mark.parametrize(
    ("config", "mapping_key"),
    [
        (lambda domain: HttpApiConfig(domain=domain, api_mapping_key="v1"), "v1"),
        (lambda domain: {"domain": domain, "api_mapping_key": "v2"}, "v2"),
    ],
    ids=["dataclass", "dict"],
)
@pulumi.runtime.test
def test_http_api_config_accepts_domain_component(
    pulumi_mocks,
    app_context_with_dns,
    config,
    mapping_key,
):
    domain = ApiDomain("shared-domain", domain_name="api.example.com")
    api = HttpApi("my-api", config=config(domain))
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources

    def check(_):
        assert_api_domain_graph(
            pulumi_mocks,
            domain_component="shared-domain",
            domain_name="api.example.com",
        )
        assert_api_mapping(
            pulumi_mocks,
            api_name="my-api",
            domain_name="api.example.com",
            mapping_key=mapping_key,
        )
        pulumi_mocks.assert_res_counts(_http_api_with_domain_counts())

    when_http_api_ready(api, check)


def test_http_api_config_conflicts_with_domain_option(app_context_with_dns):
    config_domain = ApiDomain("config-domain", domain_name="api.example.com")
    keyword_domain = ApiDomain("keyword-domain", domain_name="other.example.com")

    with raises(ValueError, match="cannot combine 'config' parameter"):
        HttpApi("my-api", config=HttpApiConfig(domain=config_domain), domain=keyword_domain)


def _when_api_domain_ready(domain: ApiDomain, callback) -> None:
    """Wait until the domain and its public DNS record are registered."""
    resources = domain.resources
    pulumi.Output.all(
        resources.custom_domain.domain_name,
        resources.custom_domain.domain_name_configuration,
        resources.dns_record.pulumi_resource.id,
    ).apply(callback)


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
        pulumi_mocks.assert_res_counts(dict(_DOMAIN_GRAPH_COUNTS))

    _when_api_domain_ready(domain, check)


@pulumi.runtime.test
def test_http_api_domain_customize_domain_key(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain(
        "shared-domain",
        domain_name="api.example.com",
        customize={"domain": {"tags": {"Purpose": "test"}}},
    )
    _ = domain.resources

    def check(_):
        assert_api_domain_graph(
            pulumi_mocks,
            domain_component="shared-domain",
            domain_name="api.example.com",
            domain_extra_inputs={"tags": {"Purpose": "test"}},
        )
        pulumi_mocks.assert_res_counts(dict(_DOMAIN_GRAPH_COUNTS))

    _when_api_domain_ready(domain, check)


def test_http_api_domain_requires_dns_provider(app_context_without_dns):
    domain = ApiDomain("shared-domain", domain_name="api.example.com")

    with raises(DnsProviderNotConfiguredError, match="DNS provider is not configured"):
        _ = domain.resources


def test_http_api_implicit_domain_name_collision(app_context_with_dns):
    ApiDomain("my-api-domain", domain_name="other.example.com")
    api = HttpApi("my-api", domain_name="api.example.com")
    api.route("GET", "/users", "functions/simple.handler")

    with raises(ValueError, match="Duplicate Stelvio component name"):
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
    with raises(ValueError, match=r"Duplicate api_mapping_key"):
        _ = api2.resources


@pulumi.runtime.test
def test_http_api_domain_duplicate_root_mapping_raises(pulumi_mocks, app_context_with_dns):
    """Two HttpApis sharing the same domain with no mapping key conflict at (root)."""
    domain = ApiDomain("shared", domain_name="api.example.com")
    api1 = HttpApi("api-one", domain=domain)
    api1.route("GET", "/users", "functions/simple.handler")
    api2 = HttpApi("api-two", domain=domain)
    api2.route("GET", "/orders", "functions/simple.handler")

    _ = api1.resources
    with raises(ValueError, match=r"Duplicate api_mapping_key \(root\)"):
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
        assert_api_domain_graph(
            pulumi_mocks,
            domain_component="shared",
            domain_name="api.example.com",
        )
        assert_api_mapping(
            pulumi_mocks,
            api_name="api-one",
            domain_name="api.example.com",
            mapping_key="v1",
        )
        assert_api_mapping(
            pulumi_mocks,
            api_name="api-two",
            domain_name="api.example.com",
            mapping_key="v2",
        )
        pulumi_mocks.assert_res_counts(
            _http_api_with_domain_counts(api_count=2, function_count=2, mapping_count=2)
        )

    when_http_api_ready([api1, api2], check)
