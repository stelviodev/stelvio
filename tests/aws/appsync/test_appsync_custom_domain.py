"""AppSync custom domain tests — ACM cert, DomainName, DNS record."""

import pulumi
import pytest

from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.dns import DnsProviderNotConfiguredError

from .conftest import COGNITO_USER_POOL_ID, INLINE_SCHEMA, when_appsync_ready

TP = "test-test-"


@pulumi.runtime.test
def test_custom_domain_creates_acm_cert(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        domain="api.example.com",
    )

    def check_resources(_):
        certs = pulumi_mocks.created_certificates()
        assert len(certs) == 1
        assert certs[0].typ == "aws:acm/certificate:Certificate"
        assert certs[0].inputs["domainName"] == "api.example.com"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_custom_domain_creates_domain_name(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        domain="api.example.com",
    )

    def check_resources(_):
        domains = pulumi_mocks.created_appsync_domain_names()
        assert len(domains) == 1
        assert domains[0].typ == "aws:appsync/domainName:DomainName"
        assert domains[0].inputs["domainName"] == "api.example.com"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_custom_domain_creates_dns_record(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        domain="api.example.com",
    )

    def check_resources(_):
        dns_records = pulumi_mocks.created_dns_records()
        domain_cname_records = [
            r
            for r in dns_records
            if r.inputs.get("type") == "CNAME" and r.inputs.get("name") == "api.example.com"
        ]
        assert len(domain_cname_records) == 1
        assert (
            domain_cname_records[0]
            .inputs["content"]
            .endswith(".appsync-api.us-east-1.amazonaws.com")
        )

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_custom_domain_creates_association(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        domain="api.example.com",
    )

    def check_resources(_):
        assocs = pulumi_mocks.created_appsync_domain_associations()
        assert len(assocs) == 1
        assert assocs[0].typ == "aws:appsync/domainNameApiAssociation:DomainNameApiAssociation"

    when_appsync_ready(api, check_resources)


@pulumi.runtime.test
def test_no_domain_creates_no_domain_resources(pulumi_mocks, project_cwd):
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
    )

    def check_resources(_):
        assert len(pulumi_mocks.created_appsync_domain_names()) == 0
        assert len(pulumi_mocks.created_appsync_domain_associations()) == 0

    when_appsync_ready(api, check_resources)


def test_custom_domain_requires_dns_provider(pulumi_mocks, project_cwd):
    """Custom domain without DNS provider configured should raise."""
    api = AppSync(
        "myapi",
        INLINE_SCHEMA,
        auth=CognitoAuth(user_pool_id=COGNITO_USER_POOL_ID),
        domain="api.example.com",
    )
    with pytest.raises(DnsProviderNotConfiguredError):
        _ = api.resources
