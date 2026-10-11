"""Durability through Function's public config, invocation, links, and subscriptions."""

import json

import pulumi
from pytest import mark, param, raises

from stelvio.aws.api_gateway import HttpApi, RestApi
from stelvio.aws.appsync import AppSync
from stelvio.aws.cloudfront import Router
from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import DurableConfig, DurableConfigDict, Function
from stelvio.aws.layer import Layer
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket
from stelvio.aws.topic import Topic
from tests.aws.pulumi_mocks import TP, R, tn
from tests.test_utils import assert_config_dict_matches_dataclass

HANDLER = "functions/simple.handler"
RUNTIME = "python3.14"
DURABLE = {"execution_timeout": 900, "retention_period": 7}


def test_cloudfront_url_permission_targets_durable_alias(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        router = Router("router")
        router.route("/api", Function("worker", handler=HANDLER, runtime=RUNTIME, durable=DURABLE))
        return router.resources

    deploy()
    pulumi_mocks.assert_res(
        "worker-cloudfront-permission-0",
        R.LAMBDA_PERMISSION,
        {"function": f"{tn(f'{TP}worker')}:live", "action": "lambda:InvokeFunctionUrl"},
        partial=True,
    )
    pulumi_mocks.assert_res(
        "worker-router-0-url", R.FUNCTION_URL, {"qualifier": "live"}, partial=True
    )


def test_durable_config_dict_matches_dataclass():
    assert_config_dict_matches_dataclass(DurableConfig, DurableConfigDict)


@mark.parametrize(
    ("options", "error", "match"),
    [
        param(
            {"runtime": None, "durable": {"execution_timeout": 1}},
            ValueError,
            "explicit runtime",
            id="default",
        ),
        param(
            {"runtime": "python3.12", "durable": DURABLE}, ValueError, "explicit runtime", id="old"
        ),
        param({"durable": True}, TypeError, "DurableConfig", id="boolean"),
        param({"durable": {}}, TypeError, "execution_timeout", id="missing-timeout"),
        param({"durable": {"execution_timeout": True}}, TypeError, "integer", id="bool-timeout"),
        param({"durable": {"execution_timeout": 0}}, ValueError, "execution_timeout", id="zero"),
        param(
            {"durable": {"execution_timeout": 31622401}}, ValueError, "execution_timeout", id="max"
        ),
        param(
            {"durable": {"execution_timeout": 1, "retention_period": 91}},
            ValueError,
            "retention_period",
            id="retention",
        ),
    ],
)
def test_durable_validation(pulumi_mocks, options, error, match):
    with raises(error, match=match):
        Function("worker", handler=HANDLER, **{"runtime": RUNTIME, **options})


def test_durable_requires_matching_layer_runtime(pulumi_mocks):
    with raises(ValueError, match="incompatible"):
        Function(
            "worker",
            handler=HANDLER,
            runtime=RUNTIME,
            durable=DURABLE,
            layers=[Layer("deps", requirements=["requests"])],
        )


@mark.parametrize("runtime", ["python3.13", "python3.14"])
def test_durable_resources_and_link(pulumi_mocks, project_cwd, runtime):
    @pulumi.runtime.test
    def deploy():
        target = Function(
            "worker",
            handler=HANDLER,
            runtime=runtime,
            durable=DurableConfig(execution_timeout=900),
            url="private",
        )
        caller = Function("caller", handler=HANDLER, links=[target])
        _ = caller.resources

        def check(values):
            raw, invocation, name, qualifier, api_invoke = values
            assert raw == f"arn:aws:lambda:us-east-1:123456789012:function:{tn(TP + 'worker')}"
            assert invocation == raw + ":live"
            assert name == tn(TP + "worker") + ":live"
            assert qualifier == "live"
            assert api_invoke.endswith(f"/functions/{invocation}/invocations")

        return pulumi.Output.all(
            target.resources.function.arn,
            target.invocation_arn,
            target.invocation_name,
            target.invocation_qualifier,
            target.invoke_arn,
        ).apply(check)

    deploy()
    pulumi_mocks.assert_res(
        "worker",
        R.FUNCTION,
        {
            "durableConfig": {"executionTimeout": 900, "retentionPeriod": 14},
            "runtime": runtime,
            "publish": True,
        },
        partial=True,
    )
    pulumi_mocks.assert_res(
        "worker-live",
        R.LAMBDA_ALIAS,
        {
            "name": "live",
            "functionName": tn(TP + "worker"),
            "functionVersion": "1",
        },
    )
    raw = f"arn:aws:lambda:us-east-1:123456789012:function:{tn(TP + 'worker')}"
    pulumi_mocks.assert_res(
        "worker-durable",
        R.ROLE_POLICY,
        {
            "role": tn(TP + "worker-r"),
            "policy": json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "lambda:CheckpointDurableExecution",
                                "lambda:GetDurableExecutionState",
                            ],
                            "Resource": (
                                f"arn:aws:lambda:us-east-1:123456789012:"
                                f"function:{TP}worker-???????:*"
                            ),
                        }
                    ],
                }
            ),
        },
    )
    pulumi_mocks.assert_res("worker-url", R.FUNCTION_URL, {"qualifier": "live"}, partial=True)
    pulumi_mocks.assert_res(
        "caller",
        R.FUNCTION,
        {
            "environment": {
                "variables": {
                    "STLV_WORKER_FUNCTION_ARN": raw + ":live",
                    "STLV_WORKER_FUNCTION_NAME": tn(TP + "worker") + ":live",
                }
            }
        },
        partial=True,
    )
    pulumi_mocks.assert_res(
        "caller-p",
        R.POLICY,
        {
            "policy": json.dumps(
                [
                    {
                        "actions": ["lambda:InvokeFunction"],
                        "resources": [raw + ":live"],
                    }
                ]
            )
        },
        partial=True,
    )


def test_durable_dev_stub_uses_selected_runtime(pulumi_mocks, project_cwd, dev_mode_context):
    @pulumi.runtime.test
    def deploy():
        return Function("worker", handler=HANDLER, runtime=RUNTIME, durable=DURABLE).resources

    deploy()
    pulumi_mocks.assert_res(
        "worker",
        R.FUNCTION,
        {
            "runtime": RUNTIME,
            "handler": "stlv_function_stub.handler",
            "durableConfig": {"executionTimeout": 900, "retentionPeriod": 7},
            "publish": True,
        },
        partial=True,
    )
    _, archive = dev_mode_context
    archive.assert_called_once_with(RUNTIME, "arm64")


def test_durable_custom_function_name_scopes_checkpoint_policy(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        return Function(
            "worker",
            handler=HANDLER,
            runtime=RUNTIME,
            durable=DURABLE,
            customize={"function": {"name": "custom-worker"}},
        ).resources

    deploy()
    policy = pulumi_mocks.assert_res("worker-durable", R.ROLE_POLICY)
    assert json.loads(policy.inputs["policy"])["Statement"][0]["Resource"] == (
        "arn:aws:lambda:us-east-1:123456789012:function:custom-worker:*"
    )


def test_durable_appsync_data_source_uses_alias(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        function = Function("worker", handler=HANDLER, runtime=RUNTIME, durable=DURABLE)
        api = AppSync("api", schema="type Query { ping: String }", auth="iam")
        source = api.data_source_lambda("worker", function)
        resolver = api.query("ping", source)
        _ = api.resources
        _ = source.resources
        return resolver.resources

    deploy()
    arn = f"arn:aws:lambda:us-east-1:123456789012:function:{tn(TP + 'worker')}:live"
    pulumi_mocks.assert_res(
        "api-ds-worker",
        R.APPSYNC_DATA_SOURCE,
        {"lambdaConfig": {"functionArn": arn}},
        partial=True,
    )
    policy = pulumi_mocks.assert_res("api-ds-worker-lambda-policy", R.ROLE_POLICY)
    assert json.loads(policy.inputs["policy"])["Statement"] == [
        {
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": arn,
        }
    ]


@mark.parametrize(
    "build",
    [
        param(lambda: Queue("jobs"), id="sqs"),
        param(
            lambda: DynamoTable(
                "records", fields={"pk": "S"}, partition_key="pk", stream="new-image"
            ),
            id="dynamo",
        ),
    ],
)
def test_durable_event_source_limit(pulumi_mocks, build):
    with raises(ValueError, match="execution_timeout <= 900"):
        build().subscribe("worker", HANDLER, runtime=RUNTIME, durable={"execution_timeout": 901})


@mark.parametrize("kind", ["http", "rest", "cron", "sns", "s3", "sqs", "dynamo"])
def test_durable_integrations_use_qualified_targets(pulumi_mocks, project_cwd, kind):
    @pulumi.runtime.test
    def deploy():
        options = {"runtime": RUNTIME, "durable": DURABLE}
        if kind in ("http", "rest"):
            api = HttpApi("api") if kind == "http" else RestApi("api")
            api.route("GET", "/", Function("worker", handler=HANDLER, **options))
            _ = api.resources
        elif kind == "cron":
            _ = Cron("clock", "rate(1 minute)", HANDLER, **options).resources
        elif kind == "sns":
            _ = Topic("events").subscribe("worker", HANDLER, **options).resources
        elif kind == "s3":
            bucket = Bucket("files")
            bucket.notify_function(
                "worker", handler=HANDLER, events=["s3:ObjectCreated:*"], **options
            )
            _ = bucket.resources
        elif kind == "sqs":
            _ = Queue("jobs").subscribe("worker", HANDLER, **options).resources
        else:
            table = DynamoTable(
                "records", fields={"pk": "S"}, partition_key="pk", stream="new-image"
            )
            _ = table.subscribe("worker", HANDLER, **options).resources

    deploy()
    function_name = {
        "http": "worker",
        "rest": "worker",
        "cron": "clock-fn",
        "sns": "events-worker",
        "s3": "files-worker",
        "sqs": "jobs-worker",
        "dynamo": "records-worker",
    }[kind]
    name = tn(TP + function_name) + ":live"
    arn = f"arn:aws:lambda:us-east-1:123456789012:function:{name}"
    expected_targets = {
        "http": (
            R.HTTP_API_INTEGRATION,
            "integrationUri",
            f"arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/{arn}/invocations",
        ),
        "rest": (
            R.API_INTEGRATION,
            "uri",
            f"arn:aws:apigateway:us-east-1:lambda:path/2015-03-31/functions/{arn}/invocations",
        ),
        "cron": (R.EVENT_TARGET, "arn", arn),
        "sns": (R.TOPIC_SUBSCRIPTION, "endpoint", arn),
        "s3": (
            R.BUCKET_NOTIFICATION,
            "lambdaFunctions",
            [{"events": ["s3:ObjectCreated:*"], "lambdaFunctionArn": arn}],
        ),
        "sqs": (R.EVENT_SOURCE_MAPPING, "functionName", name),
        "dynamo": (R.EVENT_SOURCE_MAPPING, "functionName", name),
    }
    resource_type, field, target = expected_targets[kind]
    targets = [r for r in pulumi_mocks.created_resources if r.typ == resource_type]
    assert len(targets) == 1
    pulumi_mocks.assert_res(
        targets[0].name, resource_type, {field: target}, partial=True, prefixed=False
    )
    permissions = [r for r in pulumi_mocks.created_resources if r.typ == R.LAMBDA_PERMISSION]
    assert len(permissions) == (0 if kind in ("sqs", "dynamo") else 1)
    principal = {
        "http": "apigateway.amazonaws.com",
        "rest": "apigateway.amazonaws.com",
        "cron": "events.amazonaws.com",
        "sns": "sns.amazonaws.com",
        "s3": "s3.amazonaws.com",
    }.get(kind)
    for permission in permissions:
        pulumi_mocks.assert_res(
            permission.name,
            R.LAMBDA_PERMISSION,
            {"action": "lambda:InvokeFunction", "function": name, "principal": principal},
            partial=True,
            prefixed=False,
        )
