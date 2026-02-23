import pytest

from stelvio.aws.dns import Route53Dns
from stelvio.aws.email import Email

from .assert_helpers import assert_ses_identity

pytestmark = pytest.mark.integration_dns


def _assert_email_domain_outputs(outputs: dict, name: str) -> None:
    """Assert common outputs for a domain-based Email identity (SES, DKIM)."""
    assert outputs[f"email_{name}_ses_identity_arn"]
    assert outputs[f"email_{name}_ses_domain_verification_token_arn"]
    assert outputs[f"email_{name}_ses_configuration_set_arn"]
    for i in range(3):
        assert outputs[f"email_{name}_dkim_record_{i}_name"]
        assert outputs[f"email_{name}_dkim_record_{i}_value"]


def test_email_domain_identity(stelvio_env, dns_domain, dns_zone_id):
    subdomain = f"notif.{dns_domain}"
    dns = Route53Dns(zone_id=dns_zone_id)

    def infra():
        Email("notifications", subdomain)

    outputs = stelvio_env.deploy(infra, dns=dns)

    assert_ses_identity(
        subdomain,
        identity_type="DOMAIN",
        dkim_status="SUCCESS",
        verified_for_sending=True,
    )
    _assert_email_domain_outputs(outputs, "notifications")
    assert outputs["email_notifications_dmarc_record_name"]
    assert outputs["email_notifications_dmarc_record_value"]


def test_email_domain_no_dmarc(stelvio_env, dns_domain, dns_zone_id):
    subdomain = f"alerts.{dns_domain}"
    dns = Route53Dns(zone_id=dns_zone_id)

    def infra():
        Email("alerts", subdomain, dmarc=False)

    outputs = stelvio_env.deploy(infra, dns=dns)

    assert_ses_identity(
        subdomain,
        identity_type="DOMAIN",
        dkim_status="SUCCESS",
        verified_for_sending=True,
    )
    _assert_email_domain_outputs(outputs, "alerts")
    assert "email_alerts_dmarc_record_name" not in outputs


def test_email_domain_custom_dmarc(stelvio_env, dns_domain, dns_zone_id):
    subdomain = f"strict.{dns_domain}"
    dns = Route53Dns(zone_id=dns_zone_id)

    def infra():
        Email("strict", subdomain, dmarc="v=DMARC1; p=reject;")

    outputs = stelvio_env.deploy(infra, dns=dns)

    assert_ses_identity(
        subdomain,
        identity_type="DOMAIN",
        dkim_status="SUCCESS",
        verified_for_sending=True,
    )
    _assert_email_domain_outputs(outputs, "strict")
    assert outputs["email_strict_dmarc_record_name"]
    assert outputs["email_strict_dmarc_record_value"]
