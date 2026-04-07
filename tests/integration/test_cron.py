import pytest

from stelvio.aws.cron import Cron

from .assert_helpers import (
    assert_eventbridge_rule,
    assert_eventbridge_tags,
    assert_eventbridge_target,
    assert_lambda_function,
    assert_lambda_tags,
)
from .export_helpers import export_cron, export_function

pytestmark = pytest.mark.integration


def test_cron_basic(stelvio_env, project_dir):
    def infra():
        cron = Cron("cleanup", "rate(1 day)", "handlers/echo.main")
        export_cron(cron)
        export_function(cron.resources.function)

    outputs = stelvio_env.deploy(infra)

    assert_eventbridge_rule(
        outputs["cron_cleanup_rule_arn"],
        schedule="rate(1 day)",
        state="ENABLED",
    )
    assert_lambda_function(outputs["function_cleanup-fn_arn"])


def test_cron_tags(stelvio_env, project_dir):
    def infra():
        cron = Cron("tagged-cron", "rate(1 day)", "handlers/echo.main", tags={"Team": "platform"})
        export_cron(cron)
        export_function(cron.resources.function)

    outputs = stelvio_env.deploy(infra)
    assert_eventbridge_tags(outputs["cron_tagged-cron_rule_arn"], {"Team": "platform"})
    assert_lambda_tags(outputs["function_tagged-cron-fn_arn"], {"Team": "platform"})


def test_cron_expression(stelvio_env, project_dir):
    def infra():
        cron = Cron("noon-job", "cron(0 12 * * ? *)", "handlers/echo.main")
        export_cron(cron)
        export_function(cron.resources.function)

    outputs = stelvio_env.deploy(infra)

    assert_eventbridge_rule(
        outputs["cron_noon-job_rule_arn"],
        schedule="cron(0 12 * * ? *)",
        state="ENABLED",
    )
    assert_lambda_function(outputs["function_noon-job-fn_arn"])


def test_cron_disabled(stelvio_env, project_dir):
    def infra():
        cron = Cron("paused", "rate(1 hour)", "handlers/echo.main", enabled=False)
        export_cron(cron)

    outputs = stelvio_env.deploy(infra)

    assert_eventbridge_rule(
        outputs["cron_paused_rule_arn"],
        state="DISABLED",
    )


def test_cron_payload(stelvio_env, project_dir):
    def infra():
        cron = Cron(
            "batch",
            "rate(1 day)",
            "handlers/echo.main",
            payload={"mode": "full", "batch_size": 100},
        )
        export_cron(cron)

    outputs = stelvio_env.deploy(infra)

    assert_eventbridge_rule(outputs["cron_batch_rule_arn"], state="ENABLED")
    assert_eventbridge_target(
        outputs["cron_batch_rule_arn"],
        input_payload={"mode": "full", "batch_size": 100},
    )
