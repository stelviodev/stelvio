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
            {"stage_name": "beta"},
            False,
            (f"{tid(TP + 'origin-api')[:8]}.execute-api.us-east-1.amazonaws.com", "/beta"),
        ),
        ({"domain_name": "api.example.com"}, False, ("api.example.com", None)),
        (
            {
                "domain_name": "api.example.com",
                "api_mapping_key": "v1",
            },
            False,
            ("api.example.com", "/v1"),
        ),
        (
            {
                "domain_name": "api.example.com",
                "api_mapping_key": "v1",
                "disable_execute_api_endpoint": True,
            },
            False,
            ("api.example.com", "/v1"),
        ),
        ({"api_mapping_key": "shared"}, True, ("api.example.com", "/shared")),
    ],
    ids=[
        "custom_stage",
        "custom_domain",
        "mapping_key",
        "disabled_execute_endpoint",
        "shared_domain",
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
    api_kwargs, use_shared_domain, expected_origin = case
    if use_shared_domain:
        domain = ApiDomain("shared-domain", domain_name="api.example.com")
        api = HttpApi("origin-api", domain=domain, **api_kwargs)
    else:
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
