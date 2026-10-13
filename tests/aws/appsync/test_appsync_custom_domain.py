"""AppSync custom domain tests — ACM cert, DomainName, DNS record."""

import pulumi
from pytest import mark, raises

from stelvio.aws.appsync import LambdaAuth
from stelvio.aws.function import Function
from stelvio.dns import DnsProviderNotConfiguredError

from ...conftest import TP
from ..conftest import assert_urn
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, provider_urn, tid
from .conftest import API_ID, deploy_all, make_api


def test_custom_domain_creates_resources(
    pulumi_mocks, project_cwd, app_context_with_dns, component_registry
):
    make_api(domain_name="api.example.com")

    deploy_all()

    certificate = pulumi_mocks.assert_res(
        "myapi-acm-domain-certificate",
        R.CERTIFICATE,
        {"domainName": "api.example.com"},
        partial=True,
    )
    certificate_arn = (
        f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/{tid(certificate.name)}"
    )
    pulumi_mocks.assert_res(
        "myapi-domain",
        R.APPSYNC_DOMAIN_NAME,
        {"domainName": "api.example.com", "certificateArn": certificate_arn},
    )
    pulumi_mocks.assert_res(
        "myapi-domain-assoc",
        R.APPSYNC_DOMAIN_ASSOCIATION,
        {"domainName": "api.example.com", "apiId": API_ID},
    )
    pulumi_mocks.assert_res(
        "myapi-domain-record",
        R.CLOUDFLARE_RECORD,
        {
            "zoneId": "test-zone-id",
            "type": "CNAME",
            "name": "api.example.com",
            "content": f"{tid(TP + 'myapi-domain')}.appsync-api.{DEFAULT_REGION}.amazonaws.com",
            "ttl": 1,
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.GRAPHQL_API: 1,
            R.APPSYNC_DATA_SOURCE: 1,
            R.CERTIFICATE: 1,
            R.CERTIFICATE_VALIDATION: 1,
            R.CLOUDFLARE_RECORD: 2,
            R.APPSYNC_DOMAIN_NAME: 1,
            R.APPSYNC_DOMAIN_ASSOCIATION: 1,
        }
    )


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
