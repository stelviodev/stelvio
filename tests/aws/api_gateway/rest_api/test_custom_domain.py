from collections import Counter

import pulumi
from pytest import mark, raises

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway import RestApi
from stelvio.component import ComponentRegistry
from stelvio.dns import DnsProviderNotConfiguredError

from ....conftest import TP
from ...conftest import assert_urn
from ...pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, PulumiTestMocks, R, tid
from .test_rest_api import rest_api_counts

pytestmark = mark.usefixtures("project_cwd")

DOMAIN = "api.example.com"
CUSTOM_DOMAIN_COUNTS = Counter(
    {
        R.CERTIFICATE: 1,
        R.CERTIFICATE_VALIDATION: 1,
        R.CLOUDFLARE_RECORD: 2,
        R.API_DOMAIN_NAME: 1,
        R.API_BASE_PATH_MAPPING: 1,
    }
)


def provider_urn(name: str) -> str:
    return (
        f"urn:pulumi:stack::project::pulumi:pulumi:Stack$pulumi:providers:aws::{name}::{tid(name)}"
    )


def regional_target(api_name: str) -> str:
    return f"d-{tid(f'{TP}{api_name}-custom-domain')}.execute-api.{DEFAULT_REGION}.amazonaws.com"


def assert_custom_domain(
    mocks: PulumiTestMocks,
    api_name: str,
    *,
    endpoint_type: str,
    dns_target: str,
    certificate_provider: str,
) -> None:
    """ACM certificate with its DNS validation, the API Gateway DomainName on that
    certificate, the CNAME pointing at it, and the base path mapping onto the stage.

    Edge domains are CloudFront underneath, so their certificate must sit in us-east-1:
    `certificate_provider` names the provider the certificate resources were created with.
    """
    cert_name = f"{api_name}-acm-custom-domain-certificate"
    cert_arn = f"arn:aws:acm:{DEFAULT_REGION}:{ACCOUNT_ID}:certificate/{tid(TP + cert_name)}"
    certificate = mocks.assert_res(
        cert_name, R.CERTIFICATE, {"domainName": DOMAIN, "validationMethod": "DNS"}
    )
    validation = mocks.assert_res(
        f"{cert_name}-validation",
        R.CERTIFICATE_VALIDATION,
        {"certificateArn": cert_arn, "validationRecordFqdns": [f"_test.{DOMAIN}"]},
    )
    assert certificate.provider == provider_urn(certificate_provider)
    assert validation.provider == provider_urn(certificate_provider)
    mocks.assert_res(
        f"{cert_name}-validation-record",
        R.CLOUDFLARE_RECORD,
        {
            "name": f"_test.{DOMAIN}",
            "type": "CNAME",
            "content": f"test-validation.{DOMAIN}",
            "ttl": 1,
            "zoneId": "test-zone-id",
        },
    )
    certificate_key = "certificateArn" if endpoint_type == "EDGE" else "regionalCertificateArn"
    mocks.assert_res(
        f"{api_name}-custom-domain",
        R.API_DOMAIN_NAME,
        {
            "domainName": DOMAIN,
            "endpointConfiguration": {"types": endpoint_type},
            certificate_key: cert_arn,
        },
    )
    mocks.assert_res(
        f"{api_name}-custom-domain-record",
        R.CLOUDFLARE_RECORD,
        {
            "name": DOMAIN,
            "type": "CNAME",
            "content": dns_target,
            "ttl": 1,
            "zoneId": "test-zone-id",
        },
    )
    mocks.assert_res(
        f"{api_name}-custom-domain-base-path-mapping",
        R.API_BASE_PATH_MAPPING,
        {"restApi": tid(TP + api_name), "stageName": "v1", "domainName": DOMAIN},
    )


def provider_names(mocks: PulumiTestMocks) -> set[str]:
    return {p.name for p in mocks.created(R.AWS_PROVIDER)}


def test_api_without_custom_domain(pulumi_mocks, app_context_with_dns, component_registry):
    api = RestApi("test-api-no-domain")
    api.route("GET", "/users", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    pulumi_mocks.assert_res_counts(rest_api_counts(1, 1, 1))


def test_api_custom_domain_validation_errors(app_context_with_dns, component_registry):
    with raises(TypeError, match="Domain name must be a string"):
        RestApi("test-api-1", domain_name=123)

    with raises(ValueError, match="Domain name cannot be empty"):
        RestApi("test-api-2", domain_name="")


def test_api_custom_domain_with_custom_domain(
    pulumi_mocks, app_context_with_dns, component_registry
):
    api = RestApi("test-api-1", domain_name=DOMAIN)
    api.route("GET", "/users", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_custom_domain(
        pulumi_mocks,
        "test-api-1",
        endpoint_type="REGIONAL",
        dns_target=regional_target("test-api-1"),
        certificate_provider="stelvio-aws",
    )
    assert provider_names(pulumi_mocks) == {"stelvio-aws"}
    pulumi_mocks.assert_res_counts(rest_api_counts(1, 1, 1) + CUSTOM_DOMAIN_COUNTS)


@pulumi.runtime.test
def test_api_custom_domain_parented(pulumi_mocks, app_context_with_dns, component_registry):
    """Public custom-domain CNAME and AcmValidatedDomain are parented under RestApi."""
    api = RestApi("test-api-parented", domain_name=DOMAIN)
    api.route("GET", "/users", "functions/simple.handler")
    _ = api.resources
    record = app_context_with_dns.records[-1]
    acm = next(ComponentRegistry.instances_of(AcmValidatedDomain))

    def check(urns):
        record_urn, acm_urn = urns
        assert_urn(
            record_urn,
            "stelvio:aws:RestApi",
            R.CLOUDFLARE_RECORD,
            TP + "test-api-parented-custom-domain-record",
        )
        assert_urn(
            acm_urn,
            "stelvio:aws:RestApi",
            "stelvio:aws:AcmValidatedDomain",
            "test-api-parented-acm-custom-domain",
        )

    return pulumi.Output.all(record.pulumi_resource.urn, acm.urn).apply(check)


def test_api_custom_domain_without_dns_provider(
    pulumi_mocks, app_context_without_dns, component_registry
):
    """A custom domain with no DNS provider fails when resources are created."""
    api = RestApi("test-api-3", domain_name=DOMAIN)
    api.route("GET", "/users", "functions/simple.handler")

    with raises(DnsProviderNotConfiguredError, match="DNS provider is not configured"):
        _ = api.resources


def test_edge_endpoint_acm_uses_us_east_1_provider(
    pulumi_mocks, app_context_with_dns_eu_west, component_registry
):
    """Edge endpoints are CloudFront underneath, whose certificates must be in us-east-1."""
    api = RestApi("test-api-edge", domain_name=DOMAIN, endpoint_type="edge")
    api.route("GET", "/users", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_custom_domain(
        pulumi_mocks,
        "test-api-edge",
        endpoint_type="EDGE",
        dns_target="d123456789.cloudfront.net",
        certificate_provider="stelvio-aws-us-east-1",
    )
    assert provider_names(pulumi_mocks) == {"stelvio-aws", "stelvio-aws-us-east-1"}
    pulumi_mocks.assert_res(
        "stelvio-aws-us-east-1",
        R.AWS_PROVIDER,
        {"region": "us-east-1"},
        partial=True,
        prefixed=False,
    )


def test_regional_endpoint_acm_uses_default_provider(
    pulumi_mocks, app_context_with_dns_eu_west, component_registry
):
    """Regional certificates live in the API's own region: no second provider."""
    api = RestApi("test-api-regional", domain_name=DOMAIN, endpoint_type="regional")
    api.route("GET", "/users", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_custom_domain(
        pulumi_mocks,
        "test-api-regional",
        endpoint_type="REGIONAL",
        dns_target=regional_target("test-api-regional"),
        certificate_provider="stelvio-aws",
    )
    assert provider_names(pulumi_mocks) == {"stelvio-aws"}


def test_edge_endpoint_acm_skips_provider_when_already_us_east_1(
    pulumi_mocks, app_context_with_dns, component_registry
):
    api = RestApi("test-api-edge-skip", domain_name=DOMAIN, endpoint_type="edge")
    api.route("GET", "/users", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    assert_custom_domain(
        pulumi_mocks,
        "test-api-edge-skip",
        endpoint_type="EDGE",
        dns_target="d123456789.cloudfront.net",
        certificate_provider="stelvio-aws",
    )
    assert provider_names(pulumi_mocks) == {"stelvio-aws"}
