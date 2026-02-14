import pytest

from stelvio.aws.cron import Cron

from .assert_helpers import assert_eventbridge_rule


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
