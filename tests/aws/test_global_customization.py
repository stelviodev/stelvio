"""Tests for global customization via StelvioAppConfig.customize.

These tests verify that:
1. Global customization from StelvioAppConfig applies to all component instances
2. Per-instance customization overrides global settings
3. Environment-based configuration returns correct customization per environment
4. App-wide customize is validated when StelvioAppConfig is built
"""

import ast
import importlib
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pulumi
import pulumi_aws
from pytest import mark, param, raises

import stelvio
from stelvio.aws.api_gateway import HttpApi, RestApi, WebsocketApi
from stelvio.aws.cloudfront import CloudFrontDistribution, Router
from stelvio.aws.cognito import IdentityPool, UserPool, UserPoolClient
from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function
from stelvio.aws.queue import Queue, QueueSubscription
from stelvio.aws.s3 import Bucket
from stelvio.aws.topic import Topic, TopicQueueSubscription
from stelvio.aws.vpc import Vpc
from stelvio.component import Component
from stelvio.config import AwsConfig, StelvioAppConfig

from ..conftest import TP
from .conftest import create_app_context_with_global_customize
from .pulumi_mocks import R

# =============================================================================
# Global Customization Applied to All Instances
# =============================================================================


@pulumi.runtime.test
def test_global_customize_applies_to_bucket(pulumi_mocks, project_cwd, clean_registries):
    """Test that global customization applies to Bucket instances."""
    # Arrange - set global customize for Bucket
    create_app_context_with_global_customize(
        {Bucket: {"bucket": {"force_destroy": True, "tags": {"Global": "true"}}}}
    )

    bucket1 = Bucket("bucket-one")
    bucket2 = Bucket("bucket-two")

    # Act
    _ = bucket1.resources
    _ = bucket2.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "bucket-one")
        assert len(buckets) == 1
        assert buckets[0].inputs.get("forceDestroy") is True
        assert buckets[0].inputs.get("tags") == {"Global": "true"}

        buckets2 = pulumi_mocks.created_s3_buckets(TP + "bucket-two")
        assert len(buckets2) == 1
        assert buckets2[0].inputs.get("forceDestroy") is True
        assert buckets2[0].inputs.get("tags") == {"Global": "true"}

    pulumi.Output.all(
        bucket1.resources.bucket.id,
        bucket2.resources.bucket.id,
    ).apply(check_resources)


@pulumi.runtime.test
def test_global_customize_applies_to_function(pulumi_mocks, project_cwd, clean_registries):
    """Test that global customization applies to Function instances."""
    # Arrange - set global customize for Function
    create_app_context_with_global_customize(
        {Function: {"function": {"reserved_concurrent_executions": 100}}}
    )

    fn1 = Function("fn-one", handler="functions/simple.handler")
    fn2 = Function("fn-two", handler="functions/simple.handler")

    # Act
    _ = fn1.resources
    _ = fn2.resources

    # Assert
    def check_resources(_):
        functions1 = pulumi_mocks.created_functions(TP + "fn-one")
        assert len(functions1) == 1
        assert functions1[0].inputs.get("reservedConcurrentExecutions") == 100

        functions2 = pulumi_mocks.created_functions(TP + "fn-two")
        assert len(functions2) == 1
        assert functions2[0].inputs.get("reservedConcurrentExecutions") == 100

    pulumi.Output.all(
        fn1.resources.function.id,
        fn2.resources.function.id,
    ).apply(check_resources)


@pulumi.runtime.test
def test_global_customize_multiple_component_types(pulumi_mocks, project_cwd, clean_registries):
    """Test that global customization works for multiple component types."""
    # Arrange - set global customize for multiple types
    create_app_context_with_global_customize(
        {
            Bucket: {"bucket": {"force_destroy": True}},
            Queue: {"queue": {"tags": {"GlobalQueue": "yes"}}},
            Topic: {"topic": {"tags": {"GlobalTopic": "yes"}}},
        }
    )

    bucket = Bucket("my-bucket")
    queue = Queue("my-queue")
    topic = Topic("my-topic")

    # Act
    _ = bucket.resources
    _ = queue.resources
    _ = topic.resources

    # Assert
    def check_resources(_):
        # Check bucket
        buckets = pulumi_mocks.created_s3_buckets(TP + "my-bucket")
        assert len(buckets) == 1
        assert buckets[0].inputs.get("forceDestroy") is True

        # Check queue
        queues = pulumi_mocks.created_sqs_queues(TP + "my-queue")
        assert len(queues) == 1
        assert queues[0].inputs.get("tags") == {"GlobalQueue": "yes"}

        # Check topic
        topics = [t for t in pulumi_mocks.created_sns_topics() if "my-topic" in t.name]
        assert len(topics) == 1
        assert topics[0].inputs.get("tags") == {"GlobalTopic": "yes"}

    pulumi.Output.all(
        bucket.resources.bucket.id,
        queue.resources.queue.id,
        topic.resources.topic.id,
    ).apply(check_resources)


# =============================================================================
# Per-Instance Overrides Global Customization
# =============================================================================


@pulumi.runtime.test
def test_per_instance_overrides_global_bucket(pulumi_mocks, project_cwd, clean_registries):
    """Test that per-instance customization overrides global for Bucket."""
    # Arrange - set global customize, then override per-instance
    create_app_context_with_global_customize(
        {Bucket: {"bucket": {"force_destroy": True, "tags": {"Source": "global"}}}}
    )

    # bucket1 uses global settings
    bucket1 = Bucket("bucket-global")

    # bucket2 overrides with per-instance settings
    bucket2 = Bucket(
        "bucket-override",
        customize={"bucket": {"force_destroy": False, "tags": {"Source": "instance"}}},
    )

    # Act
    _ = bucket1.resources
    _ = bucket2.resources

    # Assert
    def check_resources(_):
        # bucket1 should have global settings
        buckets1 = pulumi_mocks.created_s3_buckets(TP + "bucket-global")
        assert len(buckets1) == 1
        assert buckets1[0].inputs.get("forceDestroy") is True
        assert buckets1[0].inputs.get("tags") == {"Source": "global"}

        # bucket2 should have per-instance overrides
        buckets2 = pulumi_mocks.created_s3_buckets(TP + "bucket-override")
        assert len(buckets2) == 1
        assert buckets2[0].inputs.get("forceDestroy") is False
        assert buckets2[0].inputs.get("tags") == {"Source": "instance"}

    pulumi.Output.all(
        bucket1.resources.bucket.id,
        bucket2.resources.bucket.id,
    ).apply(check_resources)


@pulumi.runtime.test
def test_per_instance_overrides_global_function(pulumi_mocks, project_cwd, clean_registries):
    """Test that per-instance customization overrides global for Function."""
    # Arrange
    create_app_context_with_global_customize(
        {Function: {"function": {"reserved_concurrent_executions": 100, "timeout": 30}}}
    )

    # fn1 uses global settings
    fn1 = Function("fn-global", handler="functions/simple.handler")

    # fn2 overrides with per-instance
    fn2 = Function(
        "fn-override",
        handler="functions/simple.handler",
        customize={"function": {"reserved_concurrent_executions": 5}},
    )

    # Act
    _ = fn1.resources
    _ = fn2.resources

    # Assert
    def check_resources(_):
        # fn1 should have global settings
        functions1 = pulumi_mocks.created_functions(TP + "fn-global")
        assert len(functions1) == 1
        assert functions1[0].inputs.get("reservedConcurrentExecutions") == 100
        assert functions1[0].inputs.get("timeout") == 30

        # fn2 should have per-instance override (only reservedConcurrentExecutions)
        functions2 = pulumi_mocks.created_functions(TP + "fn-override")
        assert len(functions2) == 1
        # Per-instance override
        assert functions2[0].inputs.get("reservedConcurrentExecutions") == 5

    pulumi.Output.all(
        fn1.resources.function.id,
        fn2.resources.function.id,
    ).apply(check_resources)


@pulumi.runtime.test
def test_per_instance_partial_override(pulumi_mocks, project_cwd, clean_registries):
    """Test that per-instance can override specific resource while inheriting global."""
    # Arrange - global sets bucket AND public_access_block
    create_app_context_with_global_customize(
        {
            Bucket: {
                "bucket": {"force_destroy": True, "tags": {"GlobalTag": "yes"}},
            }
        }
    )

    # Per-instance only overrides bucket force_destroy, should still get global tags via merge
    bucket = Bucket("partial-override", customize={"bucket": {"force_destroy": False}})

    # Act
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "partial-override")
        assert len(buckets) == 1
        # Per-instance override should take precedence for force_destroy
        assert buckets[0].inputs.get("forceDestroy") is False
        # Global tags are preserved since per-instance customize didn't specify tags.
        # Shallow merge only replaces keys that are explicitly set in per-instance.
        assert buckets[0].inputs.get("tags") == {"GlobalTag": "yes"}

    bucket.resources.bucket.id.apply(check_resources)


@pulumi.runtime.test
def test_per_instance_tags_completely_replace_global_tags(
    pulumi_mocks, project_cwd, clean_registries
):
    """Test that per-instance tags completely replace global tags (shallow merge).

    This explicitly tests the shallow merge behavior documented in the codebase:
    when global customize sets {"tags": {"a": 1, "b": 2}} and per-instance sets
    {"tags": {"c": 3}}, the result is {"tags": {"c": 3}} - NOT a deep merge.

    This is intentional behavior but can surprise users, so we test it explicitly.
    """
    # Arrange - global sets multiple tags
    create_app_context_with_global_customize(
        {
            Bucket: {
                "bucket": {"tags": {"Team": "platform", "Cost": "shared", "ManagedBy": "stelvio"}}
            }
        }
    )

    # Per-instance sets different tags - this will REPLACE all global tags
    bucket = Bucket(
        "tags-replace-test",
        customize={"bucket": {"tags": {"Env": "dev"}}},
    )

    # Act
    _ = bucket.resources

    # Assert
    def check_resources(_):
        buckets = pulumi_mocks.created_s3_buckets(TP + "tags-replace-test")
        assert len(buckets) == 1
        tags = buckets[0].inputs.get("tags", {})

        # Shallow merge: per-instance tags COMPLETELY replace global tags
        # Only "Env" should exist - "Team", "Cost", "ManagedBy" are gone!
        assert tags == {"Env": "dev"}

        # Explicitly verify global tags are NOT present
        assert "Team" not in tags
        assert "Cost" not in tags
        assert "ManagedBy" not in tags

    bucket.resources.bucket.id.apply(check_resources)


# =============================================================================
# Environment-Based Configuration Tests
# =============================================================================


def sample_configuration(env: str) -> StelvioAppConfig:
    """Sample configuration function that returns different settings per environment."""
    if env == "dev":
        return StelvioAppConfig(
            aws=AwsConfig(profile="dev-profile", region="us-east-1"),
            customize={
                Bucket: {"bucket": {"force_destroy": True}},
                Function: {"function": {"memory_size": 256}},
            },
        )
    if env == "staging":
        return StelvioAppConfig(
            aws=AwsConfig(profile="staging-profile", region="us-west-2"),
            customize={
                Bucket: {"bucket": {"force_destroy": False}},
                Function: {"function": {"memory_size": 512}},
            },
        )
    if env == "prod":
        return StelvioAppConfig(
            aws=AwsConfig(profile="prod-profile", region="us-east-1"),
            customize={
                Function: {
                    "function": {
                        "memory_size": 1024,
                        "reserved_concurrent_executions": 100,
                    }
                },
            },
        )
    # Personal/dev environment
    return StelvioAppConfig(
        aws=AwsConfig(),
        customize={
            Bucket: {"bucket": {"force_destroy": True}},
        },
    )


def test_env_config_dev_returns_correct_customize():
    """Test that dev environment configuration has correct customize settings."""
    config = sample_configuration("dev")

    assert config.aws.profile == "dev-profile"
    assert config.aws.region == "us-east-1"
    assert Bucket in config.customize
    assert config.customize[Bucket]["bucket"]["force_destroy"] is True
    assert Function in config.customize
    assert config.customize[Function]["function"]["memory_size"] == 256


def test_env_config_staging_returns_correct_customize():
    """Test that staging environment configuration has correct customize settings."""
    config = sample_configuration("staging")

    assert config.aws.profile == "staging-profile"
    assert config.aws.region == "us-west-2"
    assert Bucket in config.customize
    assert config.customize[Bucket]["bucket"]["force_destroy"] is False
    assert config.customize[Function]["function"]["memory_size"] == 512


def test_env_config_prod_returns_correct_customize():
    """Test that prod environment configuration has correct customize settings."""
    config = sample_configuration("prod")

    assert config.aws.profile == "prod-profile"
    assert Function in config.customize
    assert config.customize[Function]["function"]["memory_size"] == 1024
    assert config.customize[Function]["function"]["reserved_concurrent_executions"] == 100
    # Prod should NOT have bucket customization
    assert Bucket not in config.customize


def test_env_config_unknown_returns_safe_defaults():
    """Test that unknown/personal environment returns safe defaults."""
    config = sample_configuration("johndoe")  # Personal username

    assert Bucket in config.customize
    assert config.customize[Bucket]["bucket"]["force_destroy"] is True
    # No function customization for personal envs
    assert Function not in config.customize


@pulumi.runtime.test
def test_env_based_customize_applied_to_components(pulumi_mocks, project_cwd, clean_registries):
    """Test that environment-specific customization is applied to components."""
    # Arrange - simulate dev environment
    dev_config = sample_configuration("dev")
    create_app_context_with_global_customize(dev_config.customize)

    bucket = Bucket("dev-bucket")
    fn = Function("dev-fn", handler="functions/simple.handler")

    # Act
    _ = bucket.resources
    _ = fn.resources

    # Assert
    def check_resources(_):
        # Check bucket has dev settings
        buckets = pulumi_mocks.created_s3_buckets(TP + "dev-bucket")
        assert len(buckets) == 1
        assert buckets[0].inputs.get("forceDestroy") is True

        # Check function has dev settings
        functions = pulumi_mocks.created_functions(TP + "dev-fn")
        assert len(functions) == 1
        assert functions[0].inputs.get("memorySize") == 256

    pulumi.Output.all(
        bucket.resources.bucket.id,
        fn.resources.function.id,
    ).apply(check_resources)


@pulumi.runtime.test
def test_env_based_prod_customize_applied(pulumi_mocks, project_cwd, clean_registries):
    """Test that prod environment customization is applied correctly."""
    # Arrange - simulate prod environment
    prod_config = sample_configuration("prod")
    create_app_context_with_global_customize(prod_config.customize)

    fn = Function("prod-fn", handler="functions/simple.handler")

    # Act
    _ = fn.resources

    # Assert
    def check_resources(_):
        functions = pulumi_mocks.created_functions(TP + "prod-fn")
        assert len(functions) == 1
        assert functions[0].inputs.get("memorySize") == 1024
        assert functions[0].inputs.get("reservedConcurrentExecutions") == 100

    fn.resources.function.id.apply(check_resources)


# =============================================================================
# Function Role and Policy Customization Tests
# =============================================================================


@pulumi.runtime.test
def test_function_role_customization(pulumi_mocks, project_cwd, clean_registries):
    """Test that role customization is applied to Lambda IAM role."""
    # Arrange
    create_app_context_with_global_customize({})

    fn = Function(
        "fn-with-role-custom",
        handler="functions/simple.handler",
        customize={
            "role": {
                "tags": {"RoleTag": "custom"},
            }
        },
    )

    # Act
    _ = fn.resources

    # Assert
    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        matching_roles = [r for r in roles if "fn-with-role-custom" in r.name]
        assert len(matching_roles) == 1
        assert matching_roles[0].inputs.get("tags") == {"RoleTag": "custom"}

    fn.resources.role.id.apply(check_resources)


@pulumi.runtime.test
def test_global_function_role_customization(pulumi_mocks, project_cwd, clean_registries):
    """Test that global role customization applies to all functions."""
    # Arrange
    create_app_context_with_global_customize(
        {
            Function: {
                "role": {"tags": {"GlobalRoleTag": "yes"}},
            }
        }
    )

    fn = Function("fn-global-role", handler="functions/simple.handler")

    # Act
    _ = fn.resources

    # Assert
    def check_resources(_):
        roles = pulumi_mocks.created_roles()
        matching_roles = [r for r in roles if "fn-global-role" in r.name]
        assert len(matching_roles) == 1
        assert matching_roles[0].inputs.get("tags") == {"GlobalRoleTag": "yes"}

    fn.resources.role.id.apply(check_resources)


# =============================================================================
# App-wide dicts replace every value Stelvio picks
# =============================================================================


@dataclass(frozen=True)
class MovedDefault:
    id: str
    component: type[Component]
    build: Callable[[], Component]
    key: str
    prop: str
    resource: str
    typ: R
    default: Any
    override: Any
    # Passes the user value, equal to `default`, as a constructor argument.
    explicit: Callable[[], Component] | None = None

    @property
    def recorded(self) -> str:
        return _recorded(self.prop)


def _recorded(prop: str) -> str:
    """`prop` as the mocks record it (camelCase)."""
    return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), prop)


def _websocket_api(customize: dict | None = None, **kwargs: Any) -> WebsocketApi:
    api = WebsocketApi("chat", customize=customize, **kwargs)
    api.route("$default", "functions/simple.handler")
    return api


def _router(**kwargs: Any) -> Router:
    router = Router("cdn", **kwargs)
    router.route("/static", Bucket("static"))
    return router


def _rest_api(customize: dict | None = None, **kwargs: Any) -> RestApi:
    api = RestApi("rest", customize=customize, **kwargs)
    api.route("GET", "/users", "functions/simple.handler")
    return api


MOVED_DEFAULTS = (
    MovedDefault(
        "dynamo-billing-mode",
        DynamoTable,
        lambda: DynamoTable("orders", fields={"id": "S"}, partition_key="id"),
        "table",
        "billing_mode",
        "orders",
        R.DYNAMO_TABLE,
        "PAY_PER_REQUEST",
        "PROVISIONED",
    ),
    MovedDefault(
        "user-pool-tier",
        UserPool,
        lambda: UserPool("users", usernames=["email"]),
        "user_pool",
        "user_pool_tier",
        "users",
        R.USER_POOL,
        "ESSENTIALS",
        "PLUS",
        explicit=lambda: UserPool("users", usernames=["email"], tier="essentials"),
    ),
    MovedDefault(
        "user-pool-deletion-protection",
        UserPool,
        lambda: UserPool("users", usernames=["email"]),
        "user_pool",
        "deletion_protection",
        "users",
        R.USER_POOL,
        "INACTIVE",
        "ACTIVE",
        explicit=lambda: UserPool("users", usernames=["email"], deletion_protection=False),
    ),
    MovedDefault(
        "user-pool-client-secret",
        UserPoolClient,
        lambda: UserPool("users", usernames=["email"]).add_client("web"),
        "client",
        "generate_secret",
        "users-web",
        R.USER_POOL_CLIENT,
        False,
        True,
        explicit=lambda: UserPool("users", usernames=["email"]).add_client(
            "web", generate_secret=False
        ),
    ),
    MovedDefault(
        "identity-pool-unauthenticated",
        IdentityPool,
        lambda: IdentityPool(
            "ids", user_pools=[{"user_pool": "us-east-1_pool123", "client": "client-id"}]
        ),
        "identity_pool",
        "allow_unauthenticated_identities",
        "ids",
        R.IDENTITY_POOL,
        False,
        True,
        explicit=lambda: IdentityPool(
            "ids",
            user_pools=[{"user_pool": "us-east-1_pool123", "client": "client-id"}],
            allow_unauthenticated=False,
        ),
    ),
    MovedDefault(
        "router-price-class",
        Router,
        _router,
        "distribution",
        "price_class",
        "cdn",
        R.DISTRIBUTION,
        "PriceClass_100",
        "PriceClass_All",
        explicit=lambda: _router(price_class="PriceClass_100"),
    ),
    MovedDefault(
        "cloudfront-price-class",
        CloudFrontDistribution,
        lambda: CloudFrontDistribution("cf", bucket=Bucket("site")),
        "distribution",
        "price_class",
        "cf",
        R.DISTRIBUTION,
        "PriceClass_100",
        "PriceClass_All",
        explicit=lambda: CloudFrontDistribution(
            "cf", bucket=Bucket("site"), price_class="PriceClass_100"
        ),
    ),
    MovedDefault(
        "http-api-log-retention",
        HttpApi,
        lambda: HttpApi("api"),
        "log_group",
        "retention_in_days",
        "api-logs",
        R.LOG_GROUP,
        30,
        14,
        explicit=lambda: HttpApi("api", access_log_retention_days=30),
    ),
    MovedDefault(
        "websocket-route-selection",
        WebsocketApi,
        _websocket_api,
        "api",
        "route_selection_expression",
        "chat",
        R.HTTP_API,
        "$request.body.action",
        "$request.body.type",
        explicit=lambda: _websocket_api(route_selection_expression="$request.body.action"),
    ),
    MovedDefault(
        "rest-api-endpoint-type",
        RestApi,
        _rest_api,
        "rest_api",
        "endpoint_configuration",
        "rest",
        R.REST_API,
        {"types": "REGIONAL"},
        {"types": "EDGE"},
        explicit=lambda: _rest_api(endpoint_type="regional"),
    ),
    MovedDefault(
        "cron-state",
        Cron,
        lambda: Cron("nightly", "rate(1 day)", "functions/simple.handler"),
        "rule",
        "state",
        "nightly-rule",
        R.EVENT_RULE,
        "ENABLED",
        "DISABLED",
        explicit=lambda: Cron("nightly", "rate(1 day)", "functions/simple.handler", enabled=True),
    ),
    MovedDefault(
        "queue-subscription-enabled",
        QueueSubscription,
        lambda: Queue("jobs").subscribe("worker", "functions/simple.handler"),
        "event_source_mapping",
        "enabled",
        "jobs-worker-subscription-mapping",
        R.EVENT_SOURCE_MAPPING,
        True,
        False,
    ),
    MovedDefault(
        "topic-queue-raw-delivery",
        TopicQueueSubscription,
        lambda: Topic("events").subscribe_queue("audit", Queue("audit")),
        "subscription",
        "raw_message_delivery",
        "events-audit-queue-subscription",
        R.TOPIC_SUBSCRIPTION,
        False,
        True,
        explicit=lambda: Topic("events").subscribe_queue(
            "audit", Queue("audit"), raw_message_delivery=False
        ),
    ),
    MovedDefault(
        "vpc-cidr",
        Vpc,
        lambda: Vpc("net"),
        "vpc",
        "cidr_block",
        "net",
        R.VPC,
        "10.0.0.0/16",
        "10.1.0.0/16",
    ),
    MovedDefault(
        "topic-fifo-dedup",
        Topic,
        lambda: Topic("events.fifo", fifo=True),
        "topic",
        "content_based_deduplication",
        "events",
        R.TOPIC,
        True,
        False,
    ),
)


@mark.parametrize("scope", ["default", "app-wide"])
@mark.parametrize("case", MOVED_DEFAULTS, ids=lambda c: c.id)
def test_app_wide_dict_replaces_stelvio_default(pulumi_mocks, project_cwd, case, scope):
    """A value Stelvio picks itself reaches the resource, and an app-wide customize dict
    replaces it. Only a value the user passed may beat the app-wide dict."""
    if scope == "app-wide":
        create_app_context_with_global_customize(
            {case.component: {case.key: {case.prop: case.override}}}
        )

    @pulumi.runtime.test
    def deploy():
        return case.build().resources

    deploy()

    expected = case.override if scope == "app-wide" else case.default
    pulumi_mocks.assert_res(case.resource, case.typ, {case.recorded: expected}, partial=True)


@mark.parametrize("case", [c for c in MOVED_DEFAULTS if c.explicit], ids=lambda c: c.id)
def test_user_value_beats_app_wide_dict(pulumi_mocks, project_cwd, case):
    # The user value equals Stelvio's default, so folding it into `x or None` would let
    # the app-wide dict win.
    create_app_context_with_global_customize(
        {case.component: {case.key: {case.prop: case.override}}}
    )

    @pulumi.runtime.test
    def deploy():
        return case.explicit().resources

    deploy()

    pulumi_mocks.assert_res(case.resource, case.typ, {case.recorded: case.default}, partial=True)


def _is_stelvio_picked(value: ast.expr) -> bool:
    if isinstance(value, ast.Dict):
        return any(_is_stelvio_picked(v) for v in value.values)
    if isinstance(value, ast.List | ast.Tuple):
        return any(_is_stelvio_picked(v) for v in value.elts)
    if isinstance(value, ast.Constant):
        return value.value is not None
    if isinstance(value, ast.JoinedStr):
        return True
    if isinstance(value, ast.BoolOp) and isinstance(value.op, ast.Or):
        fallback = value.values[-1]
        return (isinstance(fallback, ast.Constant) and fallback.value is not None) or (
            isinstance(fallback, ast.Name) and fallback.id.startswith("DEFAULT_")
        )
    return False


def test_customizer_calls_keep_stelvio_values_out_of_computed_props():
    """Lower-bound guard for the rule in `Component._customizer`: a literal, generated text
    or folded fallback in computed_props silently beats an app-wide customize dict. Dicts
    built in variables and dataclass defaults escape this scan. `tags` stay computed:
    instance tags merge into them, so defaults there would be wiped. So do a distribution's
    `origins` and `default_cache_behavior`: they carry per-instance ids, so an app-wide dict
    replacing them whole can't be useful. Wiring to resources
    Stelvio created (S3 queue-notification `policy`, cron target `rule` and `arn`) is an
    expression, so neither this scan nor the moved-defaults suite guards it."""
    root = Path(stelvio.__file__).parent
    offenders = []
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"_customizer", "customize"}
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Dict)
            ):
                continue
            computed = node.args[1]
            offenders += [
                f"{path.relative_to(root)}:{value.lineno} {ast.unparse(key)}"
                for key, value in zip(computed.keys, computed.values, strict=True)
                if key is not None
                and ast.unparse(key) not in {"'tags'", "'origins'", "'default_cache_behavior'"}
                and _is_stelvio_picked(value)
            ]
    assert offenders == []


# =============================================================================
# App-wide customize is checked when the config is built
# =============================================================================


@mark.parametrize(
    ("customize", "error", "message"),
    [
        param(
            {Cron: {"fucntion": {}}},
            ValueError,
            "Invalid app config customize for Cron: unknown key 'fucntion'. Keys that work "
            "app-wide: ['permission', 'rule', 'target']",
            id="unknown-key",
        ),
        param(
            {Cron: {"function": {"function": {"timeout": 300}}}},
            ValueError,
            "Invalid app config customize for Cron: 'function' can't be customized app-wide, "
            "only per instance: pass customize={'function': ...} in the call that creates it, "
            "like Cron(...) or topic.subscribe(...).",
            id="child-key",
        ),
        param(
            {Function: lambda p: p},
            TypeError,
            "Invalid app config customize for Function: the value must be a dict of resource "
            "keys, got function",
            id="callable-for-type",
        ),
        param(
            {"Function": {"function": {}}},
            TypeError,
            "Invalid app config customize: keys must be component types like Function, "
            "got 'Function'",
            id="key-not-a-type",
        ),
        param(
            {pulumi_aws.lambda_.Function: {"function": {}}},
            TypeError,
            "Invalid app config customize: keys must be component types like Function, "
            f"got {pulumi_aws.lambda_.Function!r}",
            id="key-not-a-component",
        ),
        param(
            {Component: {"function": {}}},
            TypeError,
            "Invalid app config customize: keys must be component types like Function, "
            f"got {Component!r}",
            id="abstract-component",
        ),
        param(
            [Function],
            TypeError,
            "Invalid app config customize: expected a dict of component type to dict, or None, "
            "got list",
            id="not-a-dict",
        ),
    ],
)
def test_invalid_app_wide_customize_raises_at_config(customize, error, message):
    with raises(error, match=re.escape(message)):
        StelvioAppConfig(customize=customize)


def test_valid_app_wide_customize_passes_config():
    customize = {
        Cron: {"rule": {"description": "nightly"}},
        Function: {"function": lambda p: p},
    }

    assert StelvioAppConfig(customize=customize).customize == customize


def _component_types(parent: type[Component] = Component) -> Iterator[type[Component]]:
    for child in parent.__subclasses__():
        yield child
        yield from _component_types(child)


def _customize_handovers() -> dict[str, set[str]]:
    """Class name -> keys read as `self._customize.get("K")`, i.e. handed to a child. Loads
    every component module on the way, so `_component_types()` sees them all."""
    root = Path(stelvio.__file__).parent
    handovers = {}
    for path in sorted(root.rglob("*.py")):
        if path == root / "component.py":
            continue
        source = path.read_text()
        if "Component[" in source:
            parts = path.relative_to(root.parent).with_suffix("").parts
            importlib.import_module(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
        for cls in ast.walk(ast.parse(source)):
            if not isinstance(cls, ast.ClassDef):
                continue
            if keys := {
                node.args[0].value
                for node in ast.walk(cls)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and "self._customize" in ast.unparse(node.func.value)
            }:
                handovers[cls.name] = keys
    return handovers


def test_app_wide_rejects_exactly_the_keys_handed_to_a_child():
    """A component that hands `self._customize.get(key)` to a child it creates must type `key`
    `ChildCustomization[...]`, else an app-wide `key` is silently ignored. Source scan: a
    component added later can't be found through the public API. Lower bound: only literal
    keys read as `self._customize.get("K")`; `self._customize["K"]` or an alias escapes it."""
    handovers = _customize_handovers()

    rejected = {}
    for component_type in _component_types():
        for key in component_type._customize_annotations() or {}:
            try:
                StelvioAppConfig(customize={component_type: {key: {}}})
            except ValueError as error:
                if "can't be customized app-wide" in str(error):
                    rejected.setdefault(component_type.__name__, set()).add(key)
    assert rejected == handovers
