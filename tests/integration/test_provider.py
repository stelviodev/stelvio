"""Tests for provider behavior: auto-tags, resource region, and component hierarchy."""

import pytest

from stelvio.app import StelvioApp
from stelvio.aws.cognito import IdentityPool, UserPool
from stelvio.aws.cognito.types import IdentityPoolBinding
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.queue import Queue
from stelvio.config import AwsConfig, StelvioAppConfig

from .assert_helpers import assert_cognito_identity_pool, assert_dynamo_tags, assert_sqs_tags
from .export_helpers import (
    export_dynamo_table,
    export_identity_pool,
    export_queue,
    export_user_pool,
)

pytestmark = pytest.mark.integration


def test_auto_tags(stelvio_env):
    """Deployed resources have stelvio:app and stelvio:env auto-tags."""

    def infra():
        queue = Queue("tasks")
        table = DynamoTable("orders", fields={"pk": "S"}, partition_key="pk")
        export_queue(queue)
        export_dynamo_table(table)

    outputs = stelvio_env.deploy(infra)

    expected_tags = {
        "stelvio:app": f"stlv-{stelvio_env.run_id}",
        "stelvio:env": "test",
    }

    # Auto-tags on SQS queue
    assert_sqs_tags(outputs["queue_tasks_url"], expected_tags)

    # Auto-tags on DynamoDB table
    assert_dynamo_tags(outputs["dynamotable_orders_arn"], expected_tags)

    # Resources are in the expected region
    region = stelvio_env.aws_region
    assert f":{region}:" in outputs["queue_tasks_arn"]
    assert f":{region}:" in outputs["dynamotable_orders_arn"]


def test_global_tags_from_app_config(stelvio_env):
    """Global app tags are merged into provider default tags and inherited by resources."""
    app = StelvioApp(f"stlv-{stelvio_env.run_id}")

    @app.config
    def config(stage):
        return StelvioAppConfig(
            aws=AwsConfig(profile=stelvio_env.aws_profile, region=stelvio_env.aws_region),
            tags={"Team": "platform", "CostCenter": "infra"},
        )

    @app.run
    def run():
        queue = Queue("tasks")
        table = DynamoTable("orders", fields={"pk": "S"}, partition_key="pk")
        export_queue(queue)
        export_dynamo_table(table)

    outputs = stelvio_env.deploy_app(app)

    expected_tags = {
        "stelvio:app": f"stlv-{stelvio_env.run_id}",
        "stelvio:env": "test",
        "Team": "platform",
        "CostCenter": "infra",
    }
    assert_sqs_tags(outputs["queue_tasks_url"], expected_tags)
    assert_dynamo_tags(outputs["dynamotable_orders_arn"], expected_tags)


def test_component_tags_override_global_tags(stelvio_env):
    """Per-component tags override global provider tags on conflicts."""
    app = StelvioApp(f"stlv-{stelvio_env.run_id}")

    @app.config
    def config(stage):
        return StelvioAppConfig(
            aws=AwsConfig(profile=stelvio_env.aws_profile, region=stelvio_env.aws_region),
            tags={"Shared": "global", "GlobalOnly": "yes"},
        )

    @app.run
    def run():
        queue = Queue("tasks", tags={"Shared": "component", "ComponentOnly": "yes"})
        table = DynamoTable("orders", fields={"pk": "S"}, partition_key="pk")
        export_queue(queue)
        export_dynamo_table(table)

    outputs = stelvio_env.deploy_app(app)

    common_auto_tags = {
        "stelvio:app": f"stlv-{stelvio_env.run_id}",
        "stelvio:env": "test",
    }

    assert_sqs_tags(
        outputs["queue_tasks_url"],
        {
            **common_auto_tags,
            "Shared": "component",
            "GlobalOnly": "yes",
            "ComponentOnly": "yes",
        },
    )
    assert_dynamo_tags(
        outputs["dynamotable_orders_arn"],
        {
            **common_auto_tags,
            "Shared": "global",
            "GlobalOnly": "yes",
        },
    )


def test_region_resolved_from_chain_when_not_configured(stelvio_env, monkeypatch):
    """With no region in AwsConfig, the region resolves via AWS_REGION and flows into
    region-derived strings.

    Regression: 0.9.0b5 interpolated a literal 'None' into IdentityPool provider names
    when region wasn't configured — and boto3 alone ignores AWS_REGION (boto/boto3#3620),
    so only-AWS_REGION setups broke even where the Pulumi provider resolved correctly.
    """
    monkeypatch.setenv("AWS_REGION", stelvio_env.aws_region)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)

    app = StelvioApp(f"stlv-{stelvio_env.run_id}")

    @app.config
    def config(stage):
        return StelvioAppConfig(
            aws=AwsConfig(profile=stelvio_env.aws_profile),  # region deliberately unset
        )

    @app.run
    def run():
        pool = UserPool("auth", usernames=["email"])
        web = pool.add_client("web")
        identity = IdentityPool(
            "main",
            user_pools=[IdentityPoolBinding(user_pool=pool, client=web)],
        )
        export_user_pool(pool)
        export_identity_pool(identity)

    outputs = stelvio_env.deploy_app(app)

    # The provider name is the exact string that contained 'None' before the fix
    region = stelvio_env.aws_region
    pool_id = outputs["user_pool_auth_id"]
    assert_cognito_identity_pool(
        outputs["identity_pool_main_id"],
        expected_provider_names=[f"cognito-idp.{region}.amazonaws.com/{pool_id}"],
    )

    # The provider itself was created with the resolved region as an explicit input
    resources = stelvio_env.export_resources()
    provider = _find_by_type(resources, "pulumi:providers:aws")
    assert provider["inputs"].get("region") == region


def test_component_hierarchy(stelvio_env):
    """Sub-resources are children of their Stelvio component in the Pulumi resource tree."""

    def infra():
        Queue("tasks")
        DynamoTable("orders", fields={"pk": "S"}, partition_key="pk")

    stelvio_env.deploy(infra)
    resources = stelvio_env.export_resources()

    # Stelvio components exist with correct type URNs
    stack = _find_by_type(resources, "pulumi:pulumi:Stack")
    queue_comp = _find_by_type(resources, "stelvio:aws:Queue")
    dynamo_comp = _find_by_type(resources, "stelvio:aws:DynamoTable")

    # Components are top-level (parented to the stack)
    assert queue_comp["parent"] == stack["urn"]
    assert dynamo_comp["parent"] == stack["urn"]

    # AWS sub-resources are children of their Stelvio component
    sqs_queue = _find_by_type_fragment(resources, "aws:sqs/queue:Queue")
    dynamo_table = _find_by_type_fragment(resources, "aws:dynamodb/table:Table")

    assert sqs_queue["parent"] == queue_comp["urn"]
    assert dynamo_table["parent"] == dynamo_comp["urn"]


def _find_by_type(resources: list[dict], resource_type: str) -> dict:
    """Find exactly one resource with the given type."""
    matches = [r for r in resources if r["type"] == resource_type]
    assert len(matches) == 1, (
        f"Expected 1 resource of type '{resource_type}', "
        f"got {len(matches)}: {[r['urn'] for r in matches]}"
    )
    return matches[0]


def _find_by_type_fragment(resources: list[dict], fragment: str) -> dict:
    """Find exactly one resource whose type contains the given fragment."""
    matches = [r for r in resources if fragment in r["type"]]
    assert len(matches) == 1, (
        f"Expected 1 resource matching '{fragment}', "
        f"got {len(matches)}: {[r['urn'] for r in matches]}"
    )
    return matches[0]
