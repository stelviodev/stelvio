import pulumi
from pytest import mark

from stelvio.aws.api_gateway import ApiDomain, HttpApi
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.router import Router

from ...conftest import TP
from ..pulumi_mocks import ACCOUNT_ID, R, tid, tn


def test_router_creates_cloudfront_origin_for_http_api(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd
):
    api = HttpApi("edge-api")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("http-router")
    router.route("/api", api)

    api_id = tid(TP + "edge-api")
    rewrite_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:function/{tn(TP + 'edge-api-uri-rewrite-0')}"

    @pulumi.runtime.test
    def deploy():
        return router.resources

    deploy()

    assert len(pulumi_mocks.created(R.HTTP_API)) == 1

    [distribution] = pulumi_mocks.created(R.DISTRIBUTION)
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

    behavior = {
        "allowedMethods": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
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
        "functionAssociations": [{"eventType": "viewer-request", "functionArn": rewrite_arn}],
    }
    # The exact path too: with `/api/*` alone, `/api` falls to the default behavior.
    assert distribution.inputs["orderedCacheBehaviors"] == [
        {"pathPattern": "/api", **behavior},
        {"pathPattern": "/api/*", **behavior},
    ]

    pulumi_mocks.assert_res(
        "edge-api-uri-rewrite-0",
        R.CLOUDFRONT_FUNCTION,
        {"code": strip_path_pattern_function_js("/api")},
        partial=True,
    )
    pulumi_mocks.assert_no_res(R.ORIGIN_ACCESS_CONTROL)


def test_http_api_origin_domain_uses_resolved_region(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd, no_region_context
):
    """Origin domain carries the chain-resolved region when config has none.

    Scenario (the default new-user setup): no `region=` in @app.config, but the AWS
    chain resolves one (here AWS_REGION=eu-central-1 via the fixture). The original
    bug interpolated the raw config value (None) into
    "<id>.execute-api.None.amazonaws.com" — CloudFront pointed at a dead host.
    """
    api = HttpApi("edge-api")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("http-router")
    router.route("/api", api)

    api_id = tid(TP + "edge-api")

    @pulumi.runtime.test
    def deploy():
        return router.resources

    deploy()

    distribution = pulumi_mocks.assert_res("http-router", R.DISTRIBUTION)
    assert distribution.inputs["origins"][0]["domainName"] == (
        f"{api_id}.execute-api.eu-central-1.amazonaws.com"
    )


@mark.parametrize(
    "case",
    [
        (
            {"stage_name": "beta"},
            False,
            (f"{tid(TP + 'origin-api')}.execute-api.us-east-1.amazonaws.com", "/beta"),
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

    @pulumi.runtime.test
    def deploy():
        return router.resources

    deploy()

    [distribution] = pulumi_mocks.created(R.DISTRIBUTION)
    origin = distribution.inputs["origins"][0]
    assert (origin["domainName"], origin.get("originPath")) == expected_origin
