from unittest.mock import Mock

import pulumi

from stelvio.aws.api_gateway import RestApi
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.origins.components.api_gateway import ApiGatewayCloudfrontAdapter
from stelvio.aws.cloudfront.router import Router

from ...conftest import TP
from ..pulumi_mocks import ACCOUNT_ID, R, tid, tn


def test_match_api_component():
    """Test that the adapter correctly identifies RestApi components."""
    mock_api = Mock(spec=RestApi)

    assert ApiGatewayCloudfrontAdapter.match(mock_api) is True

    non_api = Mock()
    assert ApiGatewayCloudfrontAdapter.match(non_api) is False


@pulumi.runtime.test
def test_router_creates_cloudfront_origin_for_rest_api(
    pulumi_mocks,
    mock_get_or_install_dependencies_function,
    project_cwd,
):
    api = RestApi("edge-api")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("rest-router")
    router.route("/api", api)
    resources = router.resources

    rewrite_arn = f"arn:aws:cloudfront::{ACCOUNT_ID}:function/{tn(TP + 'edge-api-uri-rewrite-0')}"

    def check(_):
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
                "domainName": f"{tid(TP + 'edge-api')}.execute-api.us-east-1.amazonaws.com",
                "originId": tid(TP + "edge-api"),
                "originPath": "/v1",
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
                "targetOriginId": tid(TP + "edge-api"),
                "compress": True,
                "viewerProtocolPolicy": "redirect-to-https",
                "forwardedValues": {
                    "queryString": True,
                    "cookies": {"forward": "none"},
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


@pulumi.runtime.test
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
    resources = router.resources

    def check(_):
        distribution = pulumi_mocks.assert_res("rest-router", R.DISTRIBUTION)
        assert distribution.inputs["origins"][0]["domainName"] == (
            f"{tid(TP + 'edge-api')}.execute-api.eu-central-1.amazonaws.com"
        )

    resources.distribution.id.apply(check)
