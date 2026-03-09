"""AppSync custom domain tests — ACM cert, DomainName, DNS record."""

import pulumi
import pytest

from stelvio.dns import DnsProviderNotConfiguredError

from .conftest import make_api, when_appsync_ready

TP = "test-test-"


@pytest.mark.parametrize(
    "case",
    [
        (
            lambda mocks: mocks.created_certificates(),
            "aws:acm/certificate:Certificate",
            "domainName",
            "api.example.com",
        ),
        (
            lambda mocks: mocks.created_appsync_domain_names(),
            "aws:appsync/domainName:DomainName",
            "domainName",
            "api.example.com",
        ),
        (
            lambda mocks: mocks.created_appsync_domain_associations(),
            "aws:appsync/domainNameApiAssociation:DomainNameApiAssociation",
            None,
            None,
        ),
    ],
    ids=["acm-cert", "domain-name", "association"],
)
@pulumi.runtime.test
def test_custom_domain_creates_resources(
    case, pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    resource_getter, expected_type, input_key, expected_value = case
    api = make_api(domain="api.example.com")

    def check_resources(_):
        resources = resource_getter(pulumi_mocks)
        assert len(resources) == 1
        assert resources[0].typ == expected_type
        if input_key is not None:
            assert resources[0].inputs[input_key] == expected_value

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_custom_domain_creates_dns_record(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    api = make_api(domain="api.example.com")

    def check_resources(_):
        dns_records = pulumi_mocks.created_dns_records()
        domain_cname_records = [
            r
            for r in dns_records
            if r.inputs.get("type") == "CNAME" and r.inputs.get("name") == "api.example.com"
        ]
        assert len(domain_cname_records) == 1
        assert (
            domain_cname_records[0].inputs["content"]
            == "test-test-myapi-domain-test-id.appsync-api.us-east-1.amazonaws.com"
        )

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_no_domain_creates_no_domain_resources(pulumi_mocks, project_cwd):
    api = make_api()

    def check_resources(_):
        assert len(pulumi_mocks.created_appsync_domain_names()) == 0
        assert len(pulumi_mocks.created_appsync_domain_associations()) == 0

    when_appsync_ready(api, check_resources)


def test_custom_domain_requires_dns_provider(pulumi_mocks, project_cwd):
    """Custom domain without DNS provider configured should raise."""
    api = make_api(domain="api.example.com")
    with pytest.raises(DnsProviderNotConfiguredError):
        _ = api.resources
