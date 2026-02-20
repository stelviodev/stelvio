"""Integration tests for StelvioApp-level configuration.

Tests features that require deploy_app() because they set app-level config
that deploy()'s standard setup doesn't support: global customize and link_configs.
"""

import pytest

from stelvio.app import StelvioApp
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.config import AwsConfig, StelvioAppConfig
from stelvio.link import LinkConfig

from .assert_helpers import assert_lambda_function, assert_lambda_role_permissions
from .stelvio_test_env import StelvioTestEnv

pytestmark = pytest.mark.integration


def _create_app(
    env: StelvioTestEnv,
    *,
    customize: dict | None = None,
    link_configs: dict | None = None,
) -> StelvioApp:
    """Create a StelvioApp with test AWS config and optional app-level settings."""
    kwargs = {}
    if link_configs is not None:
        kwargs["link_configs"] = link_configs

    app = StelvioApp(f"stlv-{env._run_id}", **kwargs)

    @app.config
    def config(stage):
        config_kwargs = {}
        if customize is not None:
            config_kwargs["customize"] = customize
        return StelvioAppConfig(
            aws=AwsConfig(profile=env._aws_profile, region=env._aws_region),
            **config_kwargs,
        )

    return app


# --- Global customize ---


def test_global_customize_function_timeout(stelvio_env, project_dir):
    """Global customize sets timeout on all Functions without per-instance override."""
    app = _create_app(
        stelvio_env,
        customize={Function: {"function": {"timeout": 300}}},
    )

    @app.run
    def run():
        Function("worker", handler="handlers/echo.main")

    outputs = stelvio_env.deploy_app(app)

    assert_lambda_function(outputs["function_worker_arn"], timeout=300)


def test_global_customize_overridden_by_instance(stelvio_env, project_dir):
    """Per-instance customize takes precedence over global customize."""
    app = _create_app(
        stelvio_env,
        customize={Function: {"function": {"timeout": 300, "memory_size": 512}}},
    )

    @app.run
    def run():
        Function("default", handler="handlers/echo.main")
        Function(
            "custom",
            handler="handlers/echo.main",
            customize={"function": {"timeout": 10}},
        )

    outputs = stelvio_env.deploy_app(app)

    # Global customize applies
    assert_lambda_function(outputs["function_default_arn"], timeout=300, memory=512)
    # Per-instance overrides timeout, but global memory still applies
    assert_lambda_function(outputs["function_custom_arn"], timeout=10, memory=512)


# --- Link configs ---


def _read_only_dynamo_link(table_component: DynamoTable) -> LinkConfig:
    """Custom link creator that grants read-only DynamoDB access."""
    table = table_component.resources.table
    return LinkConfig(
        properties={"table_arn": table.arn, "table_name": table.name},
        permissions=[
            AwsPermission(
                actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"],
                resources=[table.arn],
            ),
        ],
    )


def test_link_configs_override(stelvio_env, project_dir):
    """link_configs overrides default link creator for a component type."""
    app = _create_app(
        stelvio_env,
        link_configs={DynamoTable: _read_only_dynamo_link},
    )

    @app.run
    def run():
        table = DynamoTable("data", fields={"pk": "S"}, partition_key="pk")
        Function("reader", handler="handlers/echo.main", links=[table])

    outputs = stelvio_env.deploy_app(app)

    # Env vars still injected (properties unchanged)
    assert_lambda_function(
        outputs["function_reader_arn"],
        environment={
            "STLV_DATA_TABLE_ARN": outputs["dynamotable_data_arn"],
            "STLV_DATA_TABLE_NAME": outputs["dynamotable_data_name"],
        },
    )

    # Only read actions granted, write actions excluded
    assert_lambda_role_permissions(
        outputs["function_reader_role_name"],
        expected_actions=["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"],
        forbidden_actions=["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
    )
