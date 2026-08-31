"""HttpApi and WebsocketApi in one test must not share invoke-url schemes."""

import pulumi
from pytest import mark

from stelvio.aws.api_gateway import HttpApi, WebsocketApi

from ..pulumi_mocks import DEFAULT_REGION, TP, tid

pytestmark = mark.usefixtures("project_cwd")


@pulumi.runtime.test
def test_http_and_websocket_apis_keep_distinct_invoke_url_schemes(pulumi_mocks):
    http = HttpApi("mixed-http")
    http.route("GET", "/hello", "functions/simple.handler")
    ws = WebsocketApi("mixed-ws")
    ws.route("$connect", "functions/simple.handler")

    def check(urls):
        http_url, ws_invoke = urls
        assert http_url == (
            f"https://{tid(TP + 'mixed-http')}.execute-api.{DEFAULT_REGION}.amazonaws.com"
        )
        assert ws_invoke == (
            f"wss://{tid(TP + 'mixed-ws')}.execute-api.{DEFAULT_REGION}.amazonaws.com/$default"
        )

    return pulumi.Output.all(http.url, ws.resources.stage.invoke_url).apply(check)
