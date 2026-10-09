"""Which provider each part of a custom domain's certificate uses.

Pulumi replaces a resource whose provider changes, so these pin what deployed stacks have:
certificate and validation in the certificate's region, the DNS record wherever the DNS
adapter puts it.
"""

import pulumi
import pulumi_aws
import pytest
from pulumi import ResourceOptions

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway import ApiDomain, HttpApi, RestApi, WebsocketApi
from stelvio.aws.cloudfront import CloudFrontDistribution, Router
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.dns import Route53Dns
from stelvio.aws.s3 import Bucket, S3StaticWebsite
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import Record
from stelvio.provider import ProviderStore

from .appsync.conftest import make_api
from .pulumi_mocks import R, provider_urn

pytestmark = pytest.mark.usefixtures("project_cwd")


def _set_context(region: str, dns) -> None:
    _ContextStore.clear()
    ProviderStore.reset()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region=region),
            home="aws",
            dns=dns,
        )
    )


def _site(tmp_path):
    (tmp_path / "site").mkdir()
    (tmp_path / "site" / "index.html").write_text("<html></html>")
    return str(tmp_path / "site")


def _router():
    router = Router("rt", custom_domain="app.example.com")
    router.route("/", Bucket("rb"))
    return router


def _api(api):
    api.route("GET", "/users", "functions/simple.handler")
    return api


def _ws(api):
    api.route("$connect", "functions/simple.handler")
    return api


# setup -> (builder, certificate region; None = the app's region)
SETUPS = {
    "acm-no-region": (lambda _: AcmValidatedDomain("cert", domain_name="a.example.com"), None),
    "acm-us-east-1": (
        lambda _: AcmValidatedDomain("cert", domain_name="a.example.com", region="us-east-1"),
        "us-east-1",
    ),
    "acm-eu-west-1": (
        lambda _: AcmValidatedDomain("cert", domain_name="a.example.com", region="eu-west-1"),
        "eu-west-1",
    ),
    "router": (lambda _: _router(), "us-east-1"),
    "cloudfront": (
        lambda _: CloudFrontDistribution(
            "cf", bucket=Bucket("b"), custom_domain="cdn.example.com"
        ),
        "us-east-1",
    ),
    "static-website": (
        lambda tmp: S3StaticWebsite("site", directory=_site(tmp), custom_domain="www.example.com"),
        "us-east-1",
    ),
    "http-api": (lambda _: _api(HttpApi("h", domain_name="h.example.com")), None),
    "http-api-shared-domain": (
        lambda _: _api(HttpApi("h", domain=ApiDomain("d", domain_name="h.example.com"))),
        None,
    ),
    "rest-api-regional": (lambda _: _api(RestApi("r", domain_name="r.example.com")), None),
    "rest-api-edge": (
        lambda _: _api(RestApi("r", domain_name="r.example.com", endpoint_type="edge")),
        "us-east-1",
    ),
    "websocket-api": (lambda _: _ws(WebsocketApi("w", domain_name="w.example.com")), None),
    "user-pool": (
        lambda _: UserPool("u", usernames=["email"], domain="auth.example.com"),
        "us-east-1",
    ),
    "appsync": (lambda _: make_api(domain_name="gql.example.com"), "us-east-1"),
}


def _expected(app_region: str, cert_region: str | None) -> str:
    if cert_region is None or cert_region == app_region:
        return provider_urn("stelvio-aws")
    return provider_urn(f"stelvio-aws-{cert_region}")


def _deploy(component):
    @pulumi.runtime.test
    def deploy():
        return component.resources

    deploy()


# Route53 only: Pulumi ignores an AWS provider on a Cloudflare record, so Cloudflare DNS
# can't show a provider change.
@pytest.mark.parametrize("region", ["us-east-1", "eu-west-1"])
@pytest.mark.parametrize("setup", list(SETUPS))
def test_certificate_and_validation_use_the_certificate_region(
    pulumi_mocks, tmp_path, setup, region
):
    build, cert_region = SETUPS[setup]
    _set_context(region, Route53Dns(zone_id="Z1"))
    _deploy(build(tmp_path))

    [certificate] = pulumi_mocks.created(R.CERTIFICATE)
    [validation] = pulumi_mocks.created(R.CERTIFICATE_VALIDATION)
    [record] = [
        r
        for r in pulumi_mocks.created(R.ROUTE53_RECORD)
        if r.name.endswith("certificate-validation-record")
    ]
    assert certificate.provider == _expected(region, cert_region)
    assert validation.provider == _expected(region, cert_region)
    assert record.provider == provider_urn("stelvio-aws")


class CrossAccountRoute53:
    """A documented custom DNS adapter: the hosted zone lives in another AWS account.

    It keeps Stelvio's opts (parent, aliases) and supplies its own provider when the caller
    did not name one, the usual "respect the caller" merge.
    """

    def __init__(self):
        self.provider = pulumi_aws.Provider("dns-account", region="us-east-1", profile="dns")

    def create_record(  # noqa: PLR0913
        self, resource_name, name, record_type, value, ttl=1, *, opts=None
    ) -> Record:
        opts = ResourceOptions.merge(ResourceOptions(provider=self.provider), opts)
        record = pulumi_aws.route53.Record(
            resource_name,
            zone_id="Z-OTHER-ACCOUNT",
            name=name,
            type=record_type,
            records=[value],
            ttl=ttl,
            opts=opts,
        )
        return Record(record, record.name)


@pytest.mark.parametrize("setup", ["router", "http-api", "appsync", "acm-no-region"])
def test_custom_dns_adapter_keeps_its_own_provider(pulumi_mocks, tmp_path, setup):
    """The validation record goes through the adapter like every other record, so a hosted
    zone in another account keeps working."""
    _set_context("us-east-1", CrossAccountRoute53())
    _deploy(SETUPS[setup][0](tmp_path))

    records = {r.name: r.provider for r in pulumi_mocks.created(R.ROUTE53_RECORD)}
    assert records
    assert records == dict.fromkeys(records, provider_urn("dns-account"))
