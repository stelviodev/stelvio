"""Tests for WebsocketApi custom-domain behavior."""

from __future__ import annotations

import pulumi
from pytest import mark, raises

from stelvio.aws.api_gateway import ApiDomain, WebsocketApi, WebsocketApiConfig
from stelvio.dns import DnsProviderNotConfiguredError

from ...pulumi_mocks import R
from ..conftest import API_DOMAIN_GRAPH_COUNTS, assert_api_domain_graph
from .conftest import assert_api_mapping

pytestmark = mark.usefixtures("project_cwd")

_OWNED_DOMAIN_COUNTS = {
    R.HTTP_API: 1,
    R.HTTP_API_STAGE: 1,
    R.API_ACCOUNT: 2,
    R.LOG_GROUP: 1,
    R.ROLE: 2,
    R.FUNCTION: 1,
    R.ROLE_POLICY_ATTACHMENT: 1,
    R.HTTP_API_INTEGRATION: 1,
    R.LAMBDA_PERMISSION: 1,
    R.HTTP_API_ROUTE: 1,
    R.HTTP_API_MAPPING: 1,
    **API_DOMAIN_GRAPH_COUNTS,
}


@mark.parametrize(
    ("mapping_key", "expected_url"),
    [
        (None, "wss://chat.example.com"),
        ("v1", "wss://chat.example.com/v1"),
    ],
    ids=["root", "with_mapping_key"],
)
def test_websocket_api_owned_domain_creates_mapping_resource_graph(
    pulumi_mocks,
    app_context_with_dns,
    mapping_key,
    expected_url,
):
    opts = {"domain_name": "chat.example.com"}
    if mapping_key is not None:
        opts["api_mapping_key"] = mapping_key
    api = WebsocketApi("chat", **opts)
    api.route("$connect", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        def check(values):
            assert values[1] == expected_url

        return pulumi.Output.all(api.resources.api.id, api.url).apply(check)

    deploy()

    assert_api_domain_graph(
        pulumi_mocks,
        domain_component="chat-domain",
        domain_name="chat.example.com",
    )
    assert_api_mapping(
        pulumi_mocks,
        api_name="chat",
        domain_name="chat.example.com",
        mapping_key=mapping_key,
    )
    pulumi_mocks.assert_res_counts(_OWNED_DOMAIN_COUNTS)


@mark.parametrize(
    ("config", "mapping_key"),
    [
        (lambda domain: WebsocketApiConfig(domain=domain, api_mapping_key="v1"), "v1"),
        (lambda domain: {"domain": domain, "api_mapping_key": "v2"}, "v2"),
    ],
    ids=["dataclass", "dict"],
)
def test_websocket_api_config_accepts_domain_component(
    pulumi_mocks,
    app_context_with_dns,
    config,
    mapping_key,
):
    domain = ApiDomain("shared-domain", domain_name="chat.example.com")
    api = WebsocketApi("chat", config=config(domain))
    api.route("$connect", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

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
    pulumi_mocks.assert_res_counts(_OWNED_DOMAIN_COUNTS)


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


def test_websocket_api_url_with_domain_allows_adding_routes_after(
    pulumi_mocks, app_context_with_dns
):
    api = WebsocketApi("chat", domain_name="chat.example.com")
    url = api.url
    api.route("$connect", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        def check(values):
            assert values[1] == "wss://chat.example.com"

        return pulumi.Output.all(api.resources.api.id, url).apply(check)

    deploy()

    assert len(pulumi_mocks.created(R.HTTP_API_ROUTE)) == 1


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


@mark.parametrize(
    "domain_factory",
    [
        lambda: {"domain_name": "chat.example.com"},
        lambda: {"domain": ApiDomain("shared", domain_name="chat.example.com")},
    ],
    ids=["owned", "shared"],
)
def test_websocket_api_disable_execute_api_endpoint(
    pulumi_mocks, app_context_with_dns, domain_factory
):
    api = WebsocketApi("chat", disable_execute_api_endpoint=True, **domain_factory())
    api.route("$connect", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return api.resources

    deploy()

    pulumi_mocks.assert_res(
        "chat",
        R.HTTP_API,
        {
            "protocolType": "WEBSOCKET",
            "routeSelectionExpression": "$request.body.action",
            "disableExecuteApiEndpoint": True,
        },
    )


@mark.parametrize(
    ("mapping_key", "expected_error"),
    [
        ("v1", r"Duplicate api_mapping_key"),
        (None, r"Duplicate api_mapping_key \(root\)"),
    ],
    ids=["mapping_key", "root"],
)
@pulumi.runtime.test
def test_websocket_api_domain_duplicate_mapping_raises(
    pulumi_mocks, app_context_with_dns, mapping_key, expected_error
):
    domain = ApiDomain("shared", domain_name="chat.example.com")
    opts = {"domain": domain}
    if mapping_key is not None:
        opts["api_mapping_key"] = mapping_key
    api1 = WebsocketApi("api-one", **opts)
    api1.route("$connect", "functions/simple.handler")
    api2 = WebsocketApi("api-two", **opts)
    api2.route("$connect", "functions/simple.handler")

    _ = api1.resources
    with raises(ValueError, match=expected_error):
        _ = api2.resources


def test_websocket_api_domain_distinct_mapping_keys_allowed(pulumi_mocks, app_context_with_dns):
    domain = ApiDomain("shared", domain_name="chat.example.com")
    api1 = WebsocketApi("api-one", domain=domain, api_mapping_key="v1")
    api1.route("$connect", "functions/simple.handler")
    api2 = WebsocketApi("api-two", domain=domain, api_mapping_key="v2")
    api2.route("$connect", "functions/simple.handler")

    @pulumi.runtime.test
    def deploy():
        return api1.resources, api2.resources

    deploy()

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
            R.ROLE: 3,
            R.ROLE_POLICY_ATTACHMENT: 2,
            R.HTTP_API_INTEGRATION: 2,
            R.LAMBDA_PERMISSION: 2,
            R.HTTP_API_ROUTE: 2,
            R.HTTP_API_MAPPING: 2,
            R.API_ACCOUNT: 2,
            R.LOG_GROUP: 2,
            R.CERTIFICATE: 1,
            R.CLOUDFLARE_RECORD: 2,
            R.CERTIFICATE_VALIDATION: 1,
            R.HTTP_API_DOMAIN_NAME: 1,
        }
    )
