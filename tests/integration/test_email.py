import pytest

from stelvio.aws.email import Email

from .assert_helpers import assert_ses_configuration_set, assert_ses_identity, assert_ses_tags

pytestmark = pytest.mark.integration


# --- Email identity (no DNS needed) ---


def test_email_identity_basic(stelvio_env):
    def infra():
        Email("notifications", "test-integ@example.com")

    outputs = stelvio_env.deploy(infra)

    assert_ses_identity(
        "test-integ@example.com",
        identity_type="EMAIL_ADDRESS",
    )
    assert outputs["email_notifications_ses_identity_arn"]
    assert outputs["email_notifications_ses_configuration_set_arn"]


def test_email_tags(stelvio_env):
    def infra():
        Email("tagged-email", "tagged-integ@example.com", tags={"Team": "platform"})

    outputs = stelvio_env.deploy(infra)
    assert_ses_tags(outputs["email_tagged-email_ses_identity_arn"], {"Team": "platform"})
    assert_ses_tags(outputs["email_tagged-email_ses_configuration_set_arn"], {"Team": "platform"})


def test_email_identity_sandbox(stelvio_env):
    """Smoke test: sandbox=True deploys successfully.

    sandbox affects IAM link permissions (wildcard vs identity ARN),
    which is tested at the unit level. This verifies the deploy path works.
    """

    def infra():
        Email("alerts", "alerts-integ@example.com", sandbox=True)

    stelvio_env.deploy(infra)

    assert_ses_identity(
        "alerts-integ@example.com",
        identity_type="EMAIL_ADDRESS",
    )


def test_email_configuration_set(stelvio_env):
    def infra():
        Email("mailer", "mailer-integ@example.com")

    stelvio_env.deploy(infra)

    # Configuration set name follows pattern: {name}-config-set
    assert_ses_configuration_set("mailer-config-set")
