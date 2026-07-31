import pulumi
from pytest import mark

from stelvio.aws.api_gateway import ApiDomain, HttpApi
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.router import Router

from ...conftest import TP
from ..pulumi_mocks import ACCOUNT_ID, R, tid, tn


@pulumi.runtime.test
def test_router_creates_cloudfront_origin_for_http_api(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd
):
    api = HttpApi("edge-api")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("http-router")
    router.route("/api", api)
    resources = router.resources

    api_id = tid(TP + "edge-api")[:8]
    rewrite_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:function/{tn(TP + 'edge-api-uri-rewrite-0')}"

    def check(_):
        assert len(pulumi_mocks.created(R.HTTP_API)) == 1

        distributions = pulumi_mocks.created_cloudfront_distributions()
        assert len(distributions) == 1
        distribution = distributions[0]
        assert distribution.inputs["origins"] == [
            {
                "customOriginConfig": {
                    "httpPort": 80,
                    "httpsPort": 443,
                    "originProtocolPolicy": "https-only",
                    "originSslProtocols": ["TLSv1.2"],
                },
                "domainName": f"{api_id}.execute-api.us-east-1.amazonaws.com",
                "originId": api_id,
            }
        ]

        assert distribution.inputs["orderedCacheBehaviors"] == [
            {
                "pathPattern": "/api/*",
                "allowedMethods": [
                    "GET",
                    "HEAD",
                    "OPTIONS",
                    "PUT",
                    "POST",
                    "PATCH",
                    "DELETE",
                ],
                "cachedMethods": ["GET", "HEAD"],
                "targetOriginId": api_id,
                "compress": True,
                "viewerProtocolPolicy": "redirect-to-https",
                "forwardedValues": {
                    "queryString": True,
                    "cookies": {"forward": "none"},
                    "headers": ["*"],
                },
                "minTtl": 0,
                "defaultTtl": 0,
                "maxTtl": 0,
                "functionAssociations": [
                    {"eventType": "viewer-request", "functionArn": rewrite_arn},
                ],
            }
        ]

        rewrite = pulumi_mocks.created_cloudfront_functions(TP + "edge-api-uri-rewrite-0")
        assert len(rewrite) == 1
        assert rewrite[0].inputs["code"] == strip_path_pattern_function_js("/api")

        oacs = pulumi_mocks.created_origin_access_controls()
        assert len(oacs) == 0

    resources.distribution.id.apply(check)


@mark.parametrize(
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
