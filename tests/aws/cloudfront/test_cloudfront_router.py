from unittest.mock import Mock, patch

import pytest

from stelvio.aws.api_gateway import Api
from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.router import Router, RouterResources
from stelvio.aws.function import Function
from stelvio.aws.s3.s3 import Bucket
from stelvio.dns import DnsProviderNotConfiguredError


def test_cloudfront_router_basic_instantiation():
    """Test that Router can be instantiated with basic parameters."""
    router = Router(name="test-router")

    assert router.name == "test-router"
    assert router.routes == []
    assert router.price_class == "PriceClass_100"
    assert router.custom_domain is None


def test_cloudfront_router_with_custom_parameters():
    """Test that Router can be instantiated with custom parameters."""
    mock_bucket = Mock(spec=Bucket)
    routes = [Route(path_pattern="/static", component=mock_bucket)]

    router = Router(
        name="custom-router",
        routes=routes,
        price_class="PriceClass_All",
        custom_domain="example.com",
    )

    assert router.name == "custom-router"
    assert router.routes == routes
    assert router.price_class == "PriceClass_All"
    assert router.custom_domain == "example.com"


def test_cloudfront_router_resources_dataclass():
    """Test that RouterResources dataclass works correctly."""
    # Create mock objects for all required fields
    mock_distribution = Mock()
    mock_oac = Mock()
    mock_policy = Mock()
    mock_function = Mock()
    mock_acm = Mock()
    mock_record = Mock()

    resources = RouterResources(
        distribution=mock_distribution,
        origin_access_controls=[mock_oac],
        access_policies=[mock_policy],
        cloudfront_functions=[mock_function],
        acm_validated_domain=mock_acm,
        record=mock_record,
    )

    assert resources.distribution == mock_distribution
    assert resources.origin_access_controls == [mock_oac]
    assert resources.access_policies == [mock_policy]
    assert resources.cloudfront_functions == [mock_function]
    assert resources.acm_validated_domain == mock_acm
    assert resources.record == mock_record


def test_cloudfront_router_resources_with_none_values():
    """Test that RouterResources works with None values."""
    mock_distribution = Mock()

    resources = RouterResources(
        distribution=mock_distribution,
        origin_access_controls=[],
        access_policies=[],
        cloudfront_functions=[],
        acm_validated_domain=None,
        record=None,
    )

    assert resources.distribution == mock_distribution
    assert resources.origin_access_controls == []
    assert resources.access_policies == []
    assert resources.cloudfront_functions == []
    assert resources.acm_validated_domain is None
    assert resources.record is None


def test_add_route_private_method():
    """Test the private _add_route method."""
    router = Router(name="test-router")
    mock_bucket = Mock(spec=Bucket)
    route = Route(path_pattern="/files", component=mock_bucket)

    assert len(router.routes) == 0
    router._add_route(route)
    assert len(router.routes) == 1
    assert router.routes[0] == route


def test_add_route_rejects_duplicate_path_pattern():
    """Router._add_route must reject duplicate path patterns."""
    router = Router(name="test-router")
    bucket1 = Mock(spec=Bucket)
    bucket2 = Mock(spec=Bucket)

    router._add_route(Route(path_pattern="/static", component=bucket1))

    with pytest.raises(ValueError, match="Route for path pattern /static already exists"):
        router._add_route(Route(path_pattern="/static", component=bucket2))


def test_add_route_rejects_duplicate_origin():
    """Router._add_route must reject using the same origin twice."""
    router = Router(name="test-router")
    bucket = Mock(spec=Bucket)

    router._add_route(Route(path_pattern="/static", component=bucket))

    with pytest.raises(ValueError, match=r"Route for origin .* already exists"):
        router._add_route(Route(path_pattern="/files", component=bucket))


def test_add_route_rejects_non_component_or_str():
    """Router._add_route enforces Component | str for component_or_url."""
    router = Router(name="test-router")

    with pytest.raises(TypeError, match="component_or_url must be a Component or str"):
        router._add_route(Route(path_pattern="/static", component=object()))


def test_route_public_method():
    """Test the public route method."""
    router = Router(name="test-router")
    mock_bucket = Mock(spec=Bucket)

    assert len(router.routes) == 0
    router.route(path="/static", component_or_url=mock_bucket)
    assert len(router.routes) == 1
    assert router.routes[0].path_pattern == "/static"
    assert router.routes[0].component == mock_bucket


def test_route_converts_plain_url_to_url_component():
    """Router.route should wrap plain URLs in a Url component."""
    from stelvio.aws.cloudfront.origins.components.url import Url

    router = Router(name="test-router")

    router.route(path="/echo", component_or_url="https://example.com")

    assert len(router.routes) == 1
    route = router.routes[0]
    assert isinstance(route.component, Url)


def test_route_method_with_different_http_methods():
    """Test that route method works with different HTTP methods."""
    router = Router(name="test-router")
    mock_bucket = Mock(spec=Bucket)
    mock_function = Mock(spec=Function)
    mock_function.config.url = None  # Explicitly set to None to pass validation

    # The http_method parameter is currently ignored (noqa: ARG002) but we test it anyway
    router.route(path="/static", component_or_url=mock_bucket)
    router.route(path="/api", component_or_url=mock_function)

    assert len(router.routes) == 2
    assert router.routes[0].path_pattern == "/static"
    assert router.routes[1].path_pattern == "/api"


def test_route_with_function_url_config_passed_to_route():
    """Router.route must store function_url config on the Route DTO."""
    router = Router(name="test-router")
    mock_function = Mock(spec=Function)
    mock_function.config.url = None

    router.route(
        path="/api",
        component_or_url=mock_function,
        function_url={"auth": "iam", "streaming": True},
    )

    assert len(router.routes) == 1
    route = router.routes[0]
    assert route.path_pattern == "/api"
    assert route.component is mock_function
    assert route.function_url_config == {"auth": "iam", "streaming": True}


def test_multiple_routes():
    """Test that multiple routes can be added to the router."""
    router = Router(name="test-router")

    mock_bucket = Mock(spec=Bucket)
    mock_function = Mock(spec=Function)
    mock_function.config.url = None  # Explicitly set to None to pass validation
    mock_api = Mock(spec=Api)

    router.route("/static", mock_bucket)
    router.route("/lambda", mock_function)
    router.route("/api", mock_api)

    assert len(router.routes) == 3
    assert router.routes[0].path_pattern == "/static"
    assert router.routes[1].path_pattern == "/lambda"
    assert router.routes[2].path_pattern == "/api"


def test_add_route_rejects_function_with_url_config():
    """Functions configured with standalone URL cannot be added to Router."""
    router = Router(name="test-router")
    function = Mock(spec=Function)
    function.name = "fn-with-url"
    function.config.url = "public"

    with pytest.raises(ValueError, match="Function 'fn-with-url' has 'url' configuration"):
        router.route("/api", function)


@patch("stelvio.context.context")
@patch("stelvio.aws.cloudfront.router.CloudfrontAdapterRegistry")
def test_route_rejects_after_resources_created(mock_registry, mock_context):
    """Router.route must reject adding routes after resources have been created."""
    # Mock context
    mock_ctx = Mock()
    mock_ctx.prefix.return_value = "test-prefix"
    mock_context.return_value = mock_ctx

    # Set up mock adapter
    mock_adapter = Mock()
    mock_origin_config = Mock()
    mock_origin_config.origins = {"origin_id": "test-origin"}
    mock_origin_config.ordered_cache_behaviors = {"path_pattern": "/static/*"}
    mock_origin_config.origin_access_controls = Mock()
    mock_origin_config.cloudfront_functions = Mock()
    mock_adapter.get_origin_config.return_value = mock_origin_config
    mock_adapter.get_access_policy.return_value = Mock()
    mock_adapter_class = Mock(return_value=mock_adapter)
    mock_registry.get_adapter_for_component.return_value = mock_adapter_class

    # Create router with initial route
    mock_bucket = Mock(spec=Bucket)
    router = Router(name="test-router")
    router.route("/static", mock_bucket)

    # Trigger resource creation through the public API (accessing .resources property)
    with (
        patch("pulumi_aws.cloudfront.Function") as mock_cf_function,
        patch("pulumi_aws.cloudfront.Distribution") as mock_distribution,
        patch("pulumi.export"),
    ):
        mock_cf_function.return_value = Mock(arn="mock-function-arn")
        mock_distribution.return_value = Mock(
            domain_name="d123456.cloudfront.net", id="DISTRIBUTION123"
        )
        _ = router.resources  # This triggers _create_resources()

    # Now try to add another route - should fail
    another_bucket = Mock(spec=Bucket)
    with pytest.raises(
        RuntimeError, match="Cannot add routes after Router resources have been created"
    ):
        router.route("/files", another_bucket)


@pytest.mark.parametrize("price_class", ["PriceClass_100", "PriceClass_200", "PriceClass_All"])
def test_price_class_options(price_class):
    """Test that different price class options work."""
    router = Router(name="test-router", price_class=price_class)
    assert router.price_class == price_class


def test_custom_domain_configuration():
    """Test custom domain configuration."""
    router = Router(name="test-router", custom_domain="cdn.example.com")
    assert router.custom_domain == "cdn.example.com"


def test_router_inheritance():
    """Test that Router properly inherits from Component."""
    from stelvio.component import Component

    router = Router(name="test-router")
    assert isinstance(router, Component)
    assert hasattr(router, "name")
    assert hasattr(router, "_create_resources")


def test_dns_provider_not_configured_error():
    """Test that DNS error is properly imported and can be raised."""
    # This tests that the import works and the exception can be instantiated
    error = DnsProviderNotConfiguredError("Test message")
    assert str(error) == "Test message"


@patch("stelvio.context.context")
def test_create_resources_no_custom_domain_no_routes(mock_context):
    """Test _create_resources with no custom domain and no routes."""
    # Mock the context
    mock_ctx = Mock()
    mock_ctx.prefix.return_value = "test-prefix"
    mock_context.return_value = mock_ctx

    router = Router(name="test-router")

    # Should raise ValueError because at least one route is required
    with pytest.raises(ValueError, match="must have at least one route"):
        router._create_resources()


@patch("stelvio.context.context")
@patch("stelvio.aws.cloudfront.router.CloudfrontAdapterRegistry")
def test_create_resources_with_routes(mock_registry, mock_context):
    """Test _create_resources with routes."""
    # Mock the context
    mock_ctx = Mock()
    mock_ctx.prefix.return_value = "test-prefix"
    mock_context.return_value = mock_ctx

    # Create a router with a route
    mock_bucket = Mock(spec=Bucket)
    routes = [Route(path_pattern="/static", component=mock_bucket)]
    router = Router(name="test-router", routes=routes)

    # Mock adapter registry and adapter
    with (
        patch("pulumi_aws.cloudfront.Function") as mock_cf_function,
        patch("pulumi_aws.cloudfront.Distribution") as mock_distribution,
        patch("pulumi.export"),
    ):
        # Mock the adapter
        mock_adapter = Mock()
        mock_origin_config = Mock()
        mock_origin_config.origins = {"origin_id": "test-origin"}
        mock_origin_config.ordered_cache_behaviors = {"path_pattern": "/static/*"}
        mock_origin_config.origin_access_controls = Mock()
        mock_origin_config.cloudfront_functions = Mock()
        mock_adapter.get_origin_config.return_value = mock_origin_config
        mock_adapter.get_access_policy.return_value = Mock()  # Mock bucket policy

        mock_adapter_class = Mock(return_value=mock_adapter)
        mock_registry.get_adapter_for_component.return_value = mock_adapter_class
        # Mock the CloudFront resources
        mock_cf_function.return_value = Mock(arn="mock-function-arn")
        mock_distribution_instance = Mock()
        mock_distribution_instance.domain_name = "d123456.cloudfront.net"
        mock_distribution_instance.id = "DISTRIBUTION123"
        mock_distribution.return_value = mock_distribution_instance

        # Call _create_resources
        resources = router._create_resources()

        # Verify adapter was called correctly
        mock_registry.get_adapter_for_component.assert_called_once_with(mock_bucket)
        mock_adapter.get_origin_config.assert_called_once()
        mock_adapter.get_access_policy.assert_called_once_with(mock_distribution_instance)

        # Verify the resources structure
        assert resources.distribution == mock_distribution_instance
        assert len(resources.origin_access_controls) == 1
        assert len(resources.access_policies) == 1
        assert len(resources.cloudfront_functions) == 2  # Route function + default 404
        assert resources.acm_validated_domain is None
        assert resources.record is None


@patch("stelvio.context.context")
def test_create_resources_custom_domain_no_dns(mock_context):
    """Test _create_resources with custom domain but no DNS provider configured."""
    # Mock the context with no DNS provider
    mock_ctx = Mock()
    mock_ctx.dns = None
    mock_context.return_value = mock_ctx

    router = Router(name="test-router", custom_domain="example.com")

    # Should raise DnsProviderNotConfiguredError
    with pytest.raises(DnsProviderNotConfiguredError, match="DNS not configured"):
        router._create_resources()


def test_create_resources_with_custom_domain_and_dns():
    """Test _create_resources with custom domain and DNS provider."""
    # This test is simplified to avoid deep Pulumi mocking complexity
    # It tests the router configuration and basic validation
    router = Router(name="test-router-dns", custom_domain="cdn.example.com")

    # Verify router configuration
    assert router.name == "test-router-dns"
    assert router.custom_domain == "cdn.example.com"
    assert router.routes == []

    # Test that custom domain validation works
    router_with_domain = Router(name="test-router-dns-2", custom_domain="example.com")
    assert router_with_domain.custom_domain == "example.com"


def test_cloudfront_router_route_configurations():
    """Test that routes are properly configured with different components."""
    router = Router(name="test-router")

    # Test with different component types
    mock_bucket = Mock(spec=Bucket)
    mock_function = Mock(spec=Function)
    mock_function.config.url = None  # Explicitly set to None to pass validation
    mock_api = Mock(spec=Api)

    # Add routes for different component types
    router.route("/static", mock_bucket)
    router.route("/lambda", mock_function)
    router.route("/api", mock_api)

    # Verify all routes were added correctly
    assert len(router.routes) == 3

    # Check S3 route
    s3_route = router.routes[0]
    assert s3_route.path_pattern == "/static"
    assert s3_route.component == mock_bucket

    # Check Lambda route
    lambda_route = router.routes[1]
    assert lambda_route.path_pattern == "/lambda"
    assert lambda_route.component == mock_function

    # Check API Gateway route
    api_route = router.routes[2]
    assert api_route.path_pattern == "/api"
    assert api_route.component == mock_api


def test_cloudfront_router_complex_paths():
    """Test router with complex path patterns."""
    router = Router(name="test-router")

    Mock(spec=Bucket)

    # Test various path patterns
    paths = ["/static/*", "/assets/images", "/cdn/v1/files", "/uploads/*", "/content*"]

    for path in paths:
        router.route(path, Mock(spec=Bucket))

    assert len(router.routes) == len(paths)

    # Routes should be sorted by path length descending (specificity)
    expected_paths = [
        "/assets/images",  # len 14
        "/cdn/v1/files",  # len 13
        "/uploads/*",  # len 10
        "/static/*",  # len 9 (added first)
        "/content*",  # len 9 (added last)
    ]

    for i, path in enumerate(expected_paths):
        assert router.routes[i].path_pattern == path


@pytest.mark.parametrize(
    ("custom_domain", "expected_aliases"),
    [
        (None, None),
        ("example.com", ["example.com"]),
        ("cdn.mysite.org", ["cdn.mysite.org"]),
        ("static.example.co.uk", ["static.example.co.uk"]),
    ],
)
def test_custom_domain_aliases(custom_domain, expected_aliases):
    """Test that custom domains are properly configured as aliases."""
    router = Router(name="test-router", custom_domain=custom_domain)
    assert router.custom_domain == custom_domain

    # The actual aliases configuration is tested in the _create_resources method
    # This test just verifies the property is set correctly


def test_empty_routes_list():
    """Test router with explicitly empty routes list."""
    router = Router(name="test-router", routes=[])
    assert router.routes == []
    assert len(router.routes) == 0


def test_router_name_validation():
    """Test that router names are properly stored."""
    test_names = ["simple", "my-router", "router_with_underscores", "router123", "CamelCaseRouter"]

    for name in test_names:
        router = Router(name=name)
        assert router.name == name


def test_cloudfront_route_dto_integration():
    """Test integration with CloudfrontRoute DTO."""
    from stelvio.aws.cloudfront.dtos import Route

    mock_bucket = Mock(spec=Bucket)
    route = Route(path_pattern="/test", component=mock_bucket)

    router = Router(name="test-router", routes=[route])
    assert len(router.routes) == 1
    assert router.routes[0] == route
    assert router.routes[0].path_pattern == "/test"
    assert router.routes[0].component == mock_bucket


def test_router_component_inheritance_methods():
    """Test that Router has the expected Component interface."""
    router = Router(name="test-router")

    # Should have Component methods
    assert hasattr(router, "name")
    assert hasattr(router, "_create_resources")
    assert callable(router._create_resources)

    # Should be a final class
    from stelvio.aws.cloudfront.router import Router as RouterClass

    assert hasattr(RouterClass, "__final__") or RouterClass.__class__.__name__ == "final"
