"""AppSync custom domain tests — ACM cert, DomainName, DNS record."""

import pulumi
from pytest import mark, raises

from stelvio.aws.appsync import LambdaAuth
from stelvio.aws.function import Function
from stelvio.dns import DnsProviderNotConfiguredError

from ...conftest import TP
from ..conftest import assert_urn
from ..pulumi_mocks import R, provider_urn, tid
from .conftest import make_api, when_appsync_ready


@mark.parametrize(
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
    api = make_api(domain_name="api.example.com")

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
    api = make_api(domain_name="api.example.com")

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
def test_custom_domain_parented(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    api = make_api(domain_name="api.example.com")
    record = api.resources.domain_dns_record
    acm = api.resources.acm_validated_domain
    assert record is not None
    assert acm is not None

    def check(urns):
        record_urn, acm_urn = urns
        assert_urn(
            record_urn, "stelvio:aws:AppSync", R.CLOUDFLARE_RECORD, TP + "myapi-domain-record"
        )
        assert_urn(
            acm_urn, "stelvio:aws:AppSync", "stelvio:aws:AcmValidatedDomain", "myapi-acm-domain"
        )

    return pulumi.Output.all(record.pulumi_resource.urn, acm.urn).apply(check)


@pulumi.runtime.test
def test_custom_domain_url(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry, registered_outputs
):
    api = make_api(domain_name="api.example.com")

    def check(urls):
        assert urls == ["https://api.example.com/graphql"] * 2

    _ = api.resources
    return pulumi.Output.all(api.url, registered_outputs[api]["url"]).apply(check)


def test_wildcard_custom_domain_url_is_the_api_url(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry, registered_outputs
):
    # `*.example.com` names no single host to call.
    api = make_api(domain_name="*.example.com")
    urls = []

    @pulumi.runtime.test
    def deploy():
        _ = api.resources
        return pulumi.Output.all(api.url, registered_outputs[api]["url"]).apply(urls.extend)

    deploy()

    [graphql_api] = pulumi_mocks.created(R.GRAPHQL_API)
    expected = (
        f"https://appsync-{tid(graphql_api.name)}.appsync-api.us-east-1.amazonaws.com/graphql"
    )
    assert urls == [expected] * 2


def test_custom_domain_url_is_readable_while_the_api_is_being_created(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    def with_api_url(props):
        return {**props, "environment": {"variables": {"API_URL": api.url}}}

    auth_fn = Function(
        "auth-fn", handler="functions/simple.handler", customize={"function": with_api_url}
    )
    api = make_api(domain_name="api.example.com", auth=LambdaAuth(handler=auth_fn))

    @pulumi.runtime.test
    def deploy():
        _ = api.resources

    deploy()

    fn = pulumi_mocks.assert_res("auth-fn", R.FUNCTION)
    assert fn.inputs["environment"] == {
        "variables": {"API_URL": "https://api.example.com/graphql"}
    }


def test_custom_domain_certificate_is_issued_in_us_east_1(
    pulumi_mocks, project_cwd, app_context_with_dns_eu_west, component_registry
):
    """AppSync custom domains run on CloudFront, so the certificate goes to us-east-1 while
    the domain itself stays in the app's region."""
    api = make_api(domain_name="api.example.com")

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    [certificate] = pulumi_mocks.created(R.CERTIFICATE)
    [validation] = pulumi_mocks.created(R.CERTIFICATE_VALIDATION)
    [domain] = pulumi_mocks.created(R.APPSYNC_DOMAIN_NAME)
    assert certificate.provider == provider_urn("stelvio-aws-us-east-1")
    assert validation.provider == provider_urn("stelvio-aws-us-east-1")
    assert domain.provider == provider_urn("stelvio-aws")


@pulumi.runtime.test
def test_no_domain_creates_no_domain_resources(pulumi_mocks, project_cwd):
    api = make_api()

    def check_resources(_):
        assert len(pulumi_mocks.created_appsync_domain_names()) == 0
        assert len(pulumi_mocks.created_appsync_domain_associations()) == 0

    when_appsync_ready(api, check_resources)


@mark.parametrize(
    ("domain_name", "error", "match"),
    [
        (42, TypeError, "must be a string"),
        ("", ValueError, "cannot be empty"),
        ("localhost", ValueError, "at least one dot"),
        ("api.*.example.com", ValueError, "only letters, numbers, and hyphens"),
        ("*.com", ValueError, "wildcard must cover a domain with a dot"),
    ],
)
def test_custom_domain_rejects_invalid_domain_name(pulumi_mocks, domain_name, error, match):
    with raises(error, match=match):
        make_api(domain_name=domain_name)


def test_custom_domain_accepts_a_wildcard_first_label(pulumi_mocks):
    # AppSync serves every subdomain of *.example.com from one API
    assert make_api(domain_name="*.example.com").domain_name == "*.example.com"


def test_custom_domain_requires_dns_provider(pulumi_mocks, project_cwd):
    """Custom domain without DNS provider configured should raise."""
    api = make_api(domain_name="api.example.com")
    with raises(DnsProviderNotConfiguredError, match="DNS provider is not configured"):
        _ = api.resources


def test_custom_domain_customize_key(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    api = make_api(
        domain_name="api.example.com", customize={"custom_domain": {"description": "custom"}}
    )

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    pulumi_mocks.assert_res(
        "myapi-domain",
        R.APPSYNC_DOMAIN_NAME,
        {"domainName": "api.example.com", "description": "custom"},
        partial=True,
    )


@mark.parametrize(
    ("kwargs", "expected"),
    [({}, None), ({"domain_name": "api.example.com"}, "api.example.com")],
    ids=["unset", "set"],
)
def test_domain_name_property(kwargs, expected):
    assert make_api(**kwargs).domain_name == expected
