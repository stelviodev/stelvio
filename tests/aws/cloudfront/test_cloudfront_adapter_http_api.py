import pulumi
import pytest

from stelvio.aws.api_gateway.http_api import ApiDomain, HttpApi
from stelvio.aws.cloudfront.router import Router

from ...conftest import TP
from ..pulumi_mocks import tid


@pulumi.runtime.test
def test_router_creates_cloudfront_origin_for_http_api(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd
):
    api = HttpApi("edge-api")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("http-router")
    router.route("/api", api)
    resources = router.resources

    def check(_):
        apis = pulumi_mocks.created_http_apis()
        assert len(apis) == 1

        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1
        distribution = distributions[0]

        origins = distribution.inputs["origins"]
        assert len(origins) == 1
        origin = origins[0]
        assert origin["domainName"] == (
            f"{tid(TP + 'edge-api')[:8]}.execute-api.us-east-1.amazonaws.com"
        )
        assert origin.get("originPath") is None
        assert origin["customOriginConfig"]["originProtocolPolicy"] == "https-only"

        behaviors = distribution.inputs["orderedCacheBehaviors"]
        assert len(behaviors) == 1
        behavior = behaviors[0]
        assert behavior["pathPattern"] == "/api/*"
        assert behavior["allowedMethods"] == [
            "GET",
            "HEAD",
            "OPTIONS",
            "PUT",
            "POST",
            "PATCH",
            "DELETE",
        ]
        assert behavior["cachedMethods"] == ["GET", "HEAD"]
        assert behavior["forwardedValues"]["queryString"] is True
        assert behavior["forwardedValues"]["headers"] == ["*"]
        assert behavior["minTtl"] == 0
        assert behavior["defaultTtl"] == 0
        assert behavior["maxTtl"] == 0

        cloudfront_functions = pulumi_mocks.created_cloudfront_functions()
        function_names = {fn.name for fn in cloudfront_functions}
        assert f"{TP}edge-api-uri-rewrite-0" in function_names
        assert f"{TP}http-router-default-404" in function_names

        oacs = pulumi_mocks.created_origin_access_controls()
        assert len(oacs) == 0

    resources.distribution.id.apply(check)


@pytest.mark.parametrize(
    "case",
    [
        (
            {},
            (f"{tid(TP + 'origin-api')[:8]}.execute-api.us-east-1.amazonaws.com", None),
        ),
        (
            {"stage_name": "beta"},
            (f"{tid(TP + 'origin-api')[:8]}.execute-api.us-east-1.amazonaws.com", "/beta"),
        ),
        ({"domain_name": "api.example.com"}, ("api.example.com", None)),
        (
            {
                "domain_name": "api.example.com",
                "api_mapping_key": "v1",
            },
            ("api.example.com", "/v1"),
        ),
        (
            {
                "domain_name": "api.example.com",
                "api_mapping_key": "v1",
                "disable_execute_api_endpoint": True,
            },
            ("api.example.com", "/v1"),
        ),
    ],
)
@pulumi.runtime.test
def test_http_api_origin_domain_and_path(
    pulumi_mocks,
    mock_get_or_install_dependencies_function,
    project_cwd,
    app_context_with_dns,
    case,
):
    api_kwargs, expected_origin = case
    api = HttpApi("origin-api", **api_kwargs)
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("origin-router")
    router.route("/api", api)
    resources = router.resources

    def check(_):
        distribution = pulumi_mocks.created_cloudfront_distributions()[0]
        origin = distribution.inputs["origins"][0]
        assert (origin["domainName"], origin.get("originPath")) == expected_origin

    resources.distribution.id.apply(check)


@pulumi.runtime.test
def test_http_api_shared_custom_domain_origin_path(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd, app_context_with_dns
):
    domain = ApiDomain("shared-domain", domain_name="api.example.com")
    api = HttpApi("shared-api", domain=domain, api_mapping_key="shared")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("shared-domain-router")
    router.route("/api", api)
    resources = router.resources

    def check(_):
        distribution = pulumi_mocks.created_cloudfront_distributions()[0]
        origin = distribution.inputs["origins"][0]
        assert origin["domainName"] == "api.example.com"
        assert origin["originPath"] == "/shared"

    resources.distribution.id.apply(check)
