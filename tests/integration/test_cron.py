import pytest

from stelvio.aws.cron import Cron

from .assert_helpers import assert_eventbridge_rule, assert_eventbridge_target


@pytest.mark.integration
def test_cron_basic(stelvio_env, project_dir):
    def infra():
        Cron("cleanup", "rate(1 day)", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_eventbridge_rule(
        outputs["cron_cleanup_rule_arn"],
        schedule="rate(1 day)",
        state="ENABLED",
    )


@pytest.mark.integration
def test_cron_expression(stelvio_env, project_dir):
    def infra():
        Cron("noon-job", "cron(0 12 * * ? *)", "handlers/echo.main")

    outputs = stelvio_env.deploy(infra)

    assert_eventbridge_rule(
        outputs["cron_noon-job_rule_arn"],
        schedule="cron(0 12 * * ? *)",
        state="ENABLED",
    )


@pytest.mark.integration
def test_cron_disabled(stelvio_env, project_dir):
    def infra():
        Cron("paused", "rate(1 hour)", "handlers/echo.main", enabled=False)

    outputs = stelvio_env.deploy(infra)

    assert_eventbridge_rule(
        outputs["cron_paused_rule_arn"],
        state="DISABLED",
    )


@pytest.mark.integration
def test_cron_payload(stelvio_env, project_dir):
    def infra():
        Cron(
            "batch",
            "rate(1 day)",
            "handlers/echo.main",
            payload={"mode": "full", "batch_size": 100},
        )

    outputs = stelvio_env.deploy(infra)

    assert_eventbridge_rule(outputs["cron_batch_rule_arn"], state="ENABLED")
    assert_eventbridge_target(
        outputs["cron_batch_rule_arn"],
        input_payload={"mode": "full", "batch_size": 100},
    )
