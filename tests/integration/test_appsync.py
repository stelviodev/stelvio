import pytest

from stelvio.aws.appsync import ApiKeyAuth, AppSync, dynamo_get
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function

from .assert_helpers import (
    assert_appsync_api,
    assert_appsync_data_source,
    assert_appsync_resolver,
)

pytestmark = pytest.mark.integration

SCHEMA = """\
type Query {
    echo(msg: String): String
    getItem(pk: String!): Item
    listItems: [Item]
}

type Mutation {
    putItem(pk: String!, name: String): Item
}

type Item {
    pk: String!
    name: String
}
"""

PIPELINE_SCHEMA = """\
type Query {
    getPipeline(id: String!): String
}
"""


# --- Auth ---


def test_appsync_api_key_auth(stelvio_env, project_dir):
    def infra():
        api = AppSync("key-api", SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_lambda("echo", handler="handlers/appsync_echo.main")
        api.query("echo", ds)

    outputs = stelvio_env.deploy(infra)

    assert_appsync_api(
        outputs["appsync_key-api_id"],
        authentication_type="API_KEY",
    )
    assert "appsync_key-api_api_key" in outputs
    assert outputs["appsync_key-api_api_key"]


def test_appsync_iam_auth(stelvio_env, project_dir):
    def infra():
        api = AppSync("iam-api", SCHEMA, auth="iam")
        ds = api.data_source_lambda("echo", handler="handlers/appsync_echo.main")
        api.query("echo", ds)

    outputs = stelvio_env.deploy(infra)

    assert_appsync_api(
        outputs["appsync_iam-api_id"],
        authentication_type="AWS_IAM",
    )
    assert "appsync_iam-api_api_key" not in outputs


# --- Data sources ---


def test_appsync_lambda_data_source(stelvio_env, project_dir):
    def infra():
        api = AppSync("lam-ds", SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_lambda("echo", handler="handlers/appsync_echo.main")
        api.query("echo", ds)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["appsync_lam-ds_id"]

    assert_appsync_data_source(api_id, "echo", ds_type="AWS_LAMBDA", has_service_role=True)
    assert_appsync_resolver(api_id, "Query", "echo", kind="UNIT", data_source_name="echo")


def test_appsync_dynamo_data_source(stelvio_env, project_dir):
    def infra():
        table = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
        api = AppSync("dyn-ds", SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_dynamo("items", table=table)
        api.query("getItem", ds, code=dynamo_get(pk="pk"))

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["appsync_dyn-ds_id"]

    assert_appsync_data_source(api_id, "items", ds_type="AMAZON_DYNAMODB", has_service_role=True)
    assert_appsync_resolver(api_id, "Query", "getItem", kind="UNIT", data_source_name="items")


def test_appsync_http_data_source(stelvio_env, project_dir):
    def infra():
        api = AppSync("http-ds", SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_http("httpbin", url="https://httpbin.org")
        code = """\
export function request(ctx) {
    return { method: 'GET', resourcePath: '/get' };
}

export function response(ctx) {
    return JSON.parse(ctx.result.body);
}
"""
        api.query("echo", ds, code=code)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["appsync_http-ds_id"]

    assert_appsync_data_source(api_id, "httpbin", ds_type="HTTP")
    assert_appsync_resolver(api_id, "Query", "echo", kind="UNIT", data_source_name="httpbin")


# --- Resolvers ---


def test_appsync_none_resolver(stelvio_env, project_dir):
    def infra():
        api = AppSync("none-res", SCHEMA, auth=ApiKeyAuth())
        # None data source → passthrough resolver
        api.query("echo", None)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["appsync_none-res_id"]

    assert_appsync_resolver(api_id, "Query", "echo", kind="UNIT", data_source_name="NONE")


def test_appsync_pipeline_resolver(stelvio_env, project_dir):
    passthrough_code = """\
export function request(ctx) {
    return { payload: ctx.args };
}

export function response(ctx) {
    return ctx.result;
}
"""

    def infra():
        api = AppSync("pipe-res", PIPELINE_SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_lambda("echo", handler="handlers/appsync_echo.main")

        step1 = api.pipe_function("validate", None, code=passthrough_code)
        step2 = api.pipe_function("fetch", ds, code=passthrough_code)

        api.query("getPipeline", [step1, step2])

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["appsync_pipe-res_id"]

    assert_appsync_resolver(
        api_id, "Query", "getPipeline", kind="PIPELINE", pipeline_functions_count=2
    )


def test_appsync_function_instance_data_source(stelvio_env, project_dir):
    """Lambda data source using a pre-created Function instance."""

    def infra():
        fn = Function("my-echo", handler="handlers/appsync_echo.main")
        api = AppSync("fn-ds", SCHEMA, auth=ApiKeyAuth())
        ds = api.data_source_lambda("echo", handler=fn)
        api.query("echo", ds)

    outputs = stelvio_env.deploy(infra)
    api_id = outputs["appsync_fn-ds_id"]

    assert_appsync_data_source(api_id, "echo", ds_type="AWS_LAMBDA", has_service_role=True)
