from unittest.mock import Mock

import pulumi

from stelvio.aws.api_gateway.http_api import HttpApi
from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.origins.components.http_api import HttpApiCloudfrontAdapter
from stelvio.aws.cloudfront.origins.registry import CloudfrontAdapterRegistry
from stelvio.aws.cloudfront.router import Router

from ...conftest import TP


def test_http_api_adapter_basic():
    mock_api = Mock(spec=HttpApi)
    mock_api.name = "test-api"
    route = Route(path_pattern="/api", component=mock_api)

    adapter = HttpApiCloudfrontAdapter(idx=0, route=route)

    assert adapter.idx == 0
    assert adapter.route == route
    assert adapter.api == mock_api


def test_http_api_adapter_matches_http_api_components():
    mock_api = Mock(spec=HttpApi)
    non_api = Mock()

    assert HttpApiCloudfrontAdapter.match(mock_api) is True
    assert HttpApiCloudfrontAdapter.match(non_api) is False


def test_http_api_adapter_is_registered():
    CloudfrontAdapterRegistry._ensure_adapters_loaded()

    mock_api = Mock(spec=HttpApi)
    adapter_class = CloudfrontAdapterRegistry.get_adapter_for_component(mock_api)

    assert adapter_class == HttpApiCloudfrontAdapter
    assert HttpApiCloudfrontAdapter.component_class == HttpApi


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
        assert origin["domainName"].endswith(".execute-api.us-east-1.amazonaws.com")
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
        assert behavior["forwardedValues"]["queryString"] is True
        assert behavior["forwardedValues"]["headers"] == ["*"]
        assert behavior["defaultTtl"] == 0

        cloudfront_functions = pulumi_mocks.created_cloudfront_functions()
        function_names = {fn.name for fn in cloudfront_functions}
        assert f"{TP}edge-api-uri-rewrite-0" in function_names
        assert f"{TP}http-router-default-404" in function_names

        oacs = pulumi_mocks.created_origin_access_controls()
        assert len(oacs) == 0

    resources.distribution.id.apply(check)


@pulumi.runtime.test
def test_http_api_origin_path_omits_default_stage(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd
):
    api = HttpApi("default-stage-api")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("default-stage-router")
    router.route("/api", api)
    resources = router.resources

    def check(_):
        distribution = pulumi_mocks.created_cloudfront_distributions()[0]
        origin = distribution.inputs["origins"][0]
        assert origin.get("originPath") is None

    resources.distribution.id.apply(check)


@pulumi.runtime.test
def test_http_api_origin_path_uses_custom_stage(
    pulumi_mocks, mock_get_or_install_dependencies_function, project_cwd
):
    api = HttpApi("custom-stage-api", stage_name="beta")
    api.route("GET", "/users", "functions/simple.handler")

    router = Router("custom-stage-router")
    router.route("/api", api)
    resources = router.resources

    def check(_):
        distribution = pulumi_mocks.created_cloudfront_distributions()[0]
        origin = distribution.inputs["origins"][0]
        assert origin["originPath"] == "/beta"

    resources.distribution.id.apply(check)


def test_http_api_adapter_access_policy_is_not_needed():
    mock_api = Mock(spec=HttpApi)
    route = Route(path_pattern="/api", component=mock_api)
    adapter = HttpApiCloudfrontAdapter(idx=0, route=route)

    assert adapter.get_access_policy(Mock()) is None
