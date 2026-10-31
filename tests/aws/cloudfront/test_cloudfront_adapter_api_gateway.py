import pulumi

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.router import Router

from ...conftest import TP
from ..pulumi_mocks import ACCOUNT_ID, R, tid, tn


def test_router_creates_cloudfront_origin_for_rest_api(
    pulumi_mocks,
    mock_get_or_install_dependencies_function,
    project_cwd,
):
    api = RestApi("edge-api")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("rest-router")
    router.route("/api", api)

    rewrite_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:function/{tn(TP + 'rest-router-edge-api-rw')}"

    @pulumi.runtime.test
    def deploy():
        return router.resources

    deploy()

    [distribution] = pulumi_mocks.created(R.DISTRIBUTION)
    assert distribution.inputs["origins"] == [
        {
            "customOriginConfig": {
                "httpPort": 80,
                "httpsPort": 443,
                "originProtocolPolicy": "https-only",
                "originSslProtocols": ["TLSv1.2"],
            },
            "domainName": f"{tid(TP + 'edge-api')}.execute-api.us-east-1.amazonaws.com",
            "originId": tid(TP + "edge-api"),
            "originPath": "/v1",
        }
    ]

    behavior = {
        "allowedMethods": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
        "cachedMethods": ["GET", "HEAD"],
        "targetOriginId": tid(TP + "edge-api"),
        "viewerProtocolPolicy": "redirect-to-https",
        # AWS managed CachingDisabled + AllViewerExceptHostHeader
        "cachePolicyId": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
        "originRequestPolicyId": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
        "functionAssociations": [{"eventType": "viewer-request", "functionArn": rewrite_arn}],
    }
    # The exact path too: with `/api/*` alone, `/api` falls to the default behavior.
    assert distribution.inputs["orderedCacheBehaviors"] == [
        {"pathPattern": "/api", **behavior},
        {"pathPattern": "/api/*", **behavior},
    ]

    pulumi_mocks.assert_res(
        "rest-router-edge-api-rw",
        R.CLOUDFRONT_FUNCTION,
        {"code": strip_path_pattern_function_js("/api")},
        partial=True,
    )
    pulumi_mocks.assert_no_res(R.ORIGIN_ACCESS_CONTROL)


def test_rest_api_origin_domain_uses_resolved_region(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd, no_region_context
):
    """Origin domain carries the chain-resolved region when config has none.

    Scenario (the default new-user setup): no `region=` in @app.config, but the AWS
    chain resolves one (here AWS_REGION=eu-central-1 via the fixture). The original
    code asked the DEFAULT provider via a get_region() invoke — not this API's
    provider — the wrong answer under any future multi-region setup.
    """
    api = RestApi("edge-api")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("rest-router")
    router.route("/api", api)

    @pulumi.runtime.test
    def deploy():
        return router.resources

    deploy()

    distribution = pulumi_mocks.assert_res("rest-router", R.DISTRIBUTION)
    assert distribution.inputs["origins"][0]["domainName"] == (
        f"{tid(TP + 'edge-api')}.execute-api.eu-central-1.amazonaws.com"
    )
