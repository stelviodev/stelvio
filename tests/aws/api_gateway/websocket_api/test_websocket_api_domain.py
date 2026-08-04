"""Tests for WebsocketApi custom-domain behavior."""

from __future__ import annotations

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway import ApiDomain, WebsocketApi, WebsocketApiConfig
from stelvio.dns import DnsProviderNotConfiguredError

from ...pulumi_mocks import R
from .conftest import (
    assert_api_domain_graph,
    assert_api_mapping,
    websocket_api_counts,
    when_websocket_api_ready,
)

pytestmark = mark.usefixtures("project_cwd")


@pulumi.runtime.test
def test_websocket_api_owned_domain_creates_root_mapping_resource_graph(
    pulumi_mocks,
    app_context_with_dns,
):
    api = WebsocketApi("chat", domain_name="chat.example.com")
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    def check(_):
        assert_api_domain_graph(
            pulumi_mocks,
            domain_component="chat-domain",
            domain_name="chat.example.com",
        )
        assert_api_mapping(
            pulumi_mocks,
            api_name="chat",
            domain_name="chat.example.com",
        )
        pulumi_mocks.assert_res_counts(
            websocket_api_counts(
                function_count=1,
                route_count=1,
                integration_count=1,
                permission_count=1,
                mapping_count=1,
                with_domain=True,
            )
        )

    when_websocket_api_ready(api, check)


@pulumi.runtime.test
def test_websocket_api_owned_domain_with_mapping_key(
    pulumi_mocks,
    app_context_with_dns,
):
    api = WebsocketApi("chat", domain_name="chat.example.com", api_mapping_key="v1")
    api.route("$connect", "functions/simple.handler")
    resources = api.resources

    def check(values):
        url, _mapping_id = values
        assert url == "wss://chat.example.com/v1"
        assert_api_domain_graph(
            pulumi_mocks,
            domain_component="chat-domain",
            domain_name="chat.example.com",
        )
        assert_api_mapping(
            pulumi_mocks,
            api_name="chat",
            domain_name="chat.example.com",
            mapping_key="v1",
        )
        pulumi_mocks.assert_res_counts(
            websocket_api_counts(
                function_count=1,
                route_count=1,
                integration_count=1,
                permission_count=1,
                mapping_count=1,
                with_domain=True,
            )
        )

    return pulumi.Output.all(api.url, resources.api_mapping.id).apply(check)


@mark.parametrize(
    ("config", "mapping_key"),
    [
        (lambda domain: WebsocketApiConfig(domain=domain, api_mapping_key="v1"), "v1"),
        (lambda domain: {"domain": domain, "api_mapping_key": "v2"}, "v2"),
    ],
    ids=["dataclass", "dict"],
)
@pulumi.runtime.test
def test_websocket_api_config_accepts_domain_component(
    pulumi_mocks,
    app_context_with_dns,
    config,
    mapping_key,
):
    domain = ApiDomain("shared-domain", domain_name="chat.example.com")
    api = WebsocketApi("chat", config=config(domain))
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    def check(_):
        assert_api_domain_graph(
            pulumi_mocks,
            domain_component="shared-domain",
            domain_name="chat.example.com",
        )
        assert_api_mapping(
            pulumi_mocks,
            api_name="chat",
            domain_name="chat.example.com",
            mapping_key=mapping_key,
        )
        pulumi_mocks.assert_res_counts(
            websocket_api_counts(
                function_count=1,
                route_count=1,
                integration_count=1,
                permission_count=1,
                mapping_count=1,
                with_domain=True,
            )
        )

    when_websocket_api_ready(api, check)


def test_websocket_api_config_conflicts_with_domain_option(app_context_with_dns):
    config_domain = ApiDomain("config-domain", domain_name="chat.example.com")
    keyword_domain = ApiDomain("keyword-domain", domain_name="other.example.com")

    with raises(ValueError, match="cannot combine 'config' parameter"):
        WebsocketApi(
            "chat",
            config=WebsocketApiConfig(domain=config_domain),
            domain=keyword_domain,
        )


@pulumi.runtime.test
def test_websocket_api_url_with_domain_name(pulumi_mocks, app_context_with_dns):
    api = WebsocketApi("chat", domain_name="chat.example.com")
    api.route("$connect", "functions/simple.handler")

    def check(url):
        assert url == "wss://chat.example.com"

    api.url.apply(check)


@pulumi.runtime.test
def test_websocket_api_url_with_domain_allows_adding_routes_after(
    pulumi_mocks, app_context_with_dns
):
    api = WebsocketApi("chat", domain_name="chat.example.com")
    url = api.url
    api.route("$connect", "functions/simple.handler")

    def check_route_created(_):
        assert len(pulumi_mocks.created(R.HTTP_API_ROUTE)) == 1

    def check_url(resolved):
        assert resolved == "wss://chat.example.com"

    when_websocket_api_ready(api, check_route_created)
    url.apply(check_url)


@pulumi.runtime.test
def test_websocket_api_url_with_shared_domain_and_mapping_key(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain("shared", domain_name="chat.example.com")
    api = WebsocketApi("chat", domain=domain, api_mapping_key="v2")
    api.route("$connect", "functions/simple.handler")

    def check(url):
        assert url == "wss://chat.example.com/v2"

    api.url.apply(check)


def test_websocket_api_public_domain_properties(app_context_with_dns):
    domain = ApiDomain("shared", domain_name="chat.example.com")
    api = WebsocketApi("chat", domain=domain, api_mapping_key="v2")

    assert api.domain_name == "chat.example.com"


@mark.parametrize(
    ("action", "expected_error"),
    [
        (
            lambda: WebsocketApiConfig(
                domain=ApiDomain("shared-domain", domain_name="chat.example.com"),
                domain_name="other.example.com",
            ),
            "Cannot specify both 'domain_name' and 'domain'",
        ),
        (lambda: WebsocketApi("chat", api_mapping_key="v1"), "api_mapping_key requires"),
        (
            lambda: WebsocketApiConfig(disable_execute_api_endpoint=True),
            "disable_execute_api_endpoint=True requires",
        ),
    ],
    ids=[
        "domain_name_and_domain",
        "mapping_key_without_domain",
        "disable_execute_without_domain",
    ],
)
def test_websocket_api_rejects_invalid_domain_configuration(action, expected_error):
    with raises(ValueError, match=expected_error):
        action()


@mark.parametrize("bad_key", ["/v1", "v1/", "a//b", ""])
def test_websocket_api_invalid_mapping_key_raises(bad_key, app_context_with_dns):
    with raises(ValueError, match="api_mapping_key"):
        WebsocketApi("chat", domain_name="chat.example.com", api_mapping_key=bad_key)


@mark.parametrize("domain_name", ["", "   "])
def test_websocket_api_rejects_empty_domain_name(domain_name):
    with raises(ValueError, match="Domain name cannot be empty"):
        WebsocketApi("chat", domain_name=domain_name)


@mark.parametrize("domain_name", [123, [], {}, True])
def test_websocket_api_rejects_invalid_domain_name_type(domain_name):
    with raises(TypeError, match="Domain name must be a string"):
        WebsocketApi("chat", domain_name=domain_name)  # type: ignore[arg-type]


def test_websocket_api_domain_requires_dns_provider(app_context_without_dns):
    api = WebsocketApi("chat", domain_name="chat.example.com")
    api.route("$connect", "functions/simple.handler")

    with raises(DnsProviderNotConfiguredError, match="DNS provider is not configured"):
        _ = api.resources


def test_websocket_api_implicit_domain_name_collision(app_context_with_dns):
    ApiDomain("chat-domain", domain_name="other.example.com")
    api = WebsocketApi("chat", domain_name="chat.example.com")
    api.route("$connect", "functions/simple.handler")

    with raises(ValueError, match="Duplicate Stelvio component name"):
        _ = api.resources


@pulumi.runtime.test
def test_websocket_api_disable_execute_api_endpoint(pulumi_mocks, app_context_with_dns):
    api = WebsocketApi("chat", domain_name="chat.example.com", disable_execute_api_endpoint=True)
    api.route("$connect", "functions/simple.handler")
    _ = api.resources

    def check(_):
        pulumi_mocks.assert_res(
            "chat",
            R.HTTP_API,
            {
                "protocolType": "WEBSOCKET",
                "disableExecuteApiEndpoint": True,
            },
            partial=True,
        )

    when_websocket_api_ready(api, check)


@pulumi.runtime.test
def test_websocket_api_domain_duplicate_mapping_key_raises(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain("shared", domain_name="chat.example.com")
    api1 = WebsocketApi("api-one", domain=domain, api_mapping_key="v1")
    api1.route("$connect", "functions/simple.handler")
    api2 = WebsocketApi("api-two", domain=domain, api_mapping_key="v1")
    api2.route("$connect", "functions/simple.handler")

    _ = api1.resources
    with raises(ValueError, match=r"Duplicate api_mapping_key"):
        _ = api2.resources


@pulumi.runtime.test
def test_websocket_api_domain_duplicate_root_mapping_raises(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain("shared", domain_name="chat.example.com")
    api1 = WebsocketApi("api-one", domain=domain)
    api1.route("$connect", "functions/simple.handler")
    api2 = WebsocketApi("api-two", domain=domain)
    api2.route("$connect", "functions/simple.handler")

    _ = api1.resources
    with raises(ValueError, match=r"Duplicate api_mapping_key \(root\)"):
        _ = api2.resources


@pulumi.runtime.test
def test_websocket_api_domain_distinct_mapping_keys_allowed(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain("shared", domain_name="chat.example.com")
    api1 = WebsocketApi("api-one", domain=domain, api_mapping_key="v1")
    api1.route("$connect", "functions/simple.handler")
    api2 = WebsocketApi("api-two", domain=domain, api_mapping_key="v2")
    api2.route("$connect", "functions/simple.handler")

    _ = api1.resources
    _ = api2.resources

    def check(_):
        assert_api_domain_graph(
            pulumi_mocks,
            domain_component="shared",
            domain_name="chat.example.com",
        )
        assert_api_mapping(
            pulumi_mocks,
            api_name="api-one",
            domain_name="chat.example.com",
            mapping_key="v1",
        )
        assert_api_mapping(
            pulumi_mocks,
            api_name="api-two",
            domain_name="chat.example.com",
            mapping_key="v2",
        )
        pulumi_mocks.assert_res_counts(
            {
                R.HTTP_API: 2,
                R.HTTP_API_STAGE: 2,
                R.FUNCTION: 2,
                R.ROLE: 2,
                R.ROLE_POLICY_ATTACHMENT: 2,
                R.HTTP_API_INTEGRATION: 2,
                R.LAMBDA_PERMISSION: 2,
                R.HTTP_API_ROUTE: 2,
                R.HTTP_API_MAPPING: 2,
                R.CERTIFICATE: 1,
                R.CLOUDFLARE_RECORD: 2,
                R.CERTIFICATE_VALIDATION: 1,
                R.HTTP_API_DOMAIN_NAME: 1,
            }
        )

    when_websocket_api_ready([api1, api2], check)
