import pytest

from stelvio.aws.dns import Route53Dns
from stelvio.aws.email import Email

from .assert_helpers import assert_ses_identity

pytestmark = pytest.mark.integration_dns


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
    assert outputs["notifications-ses-identity-arn"]
    assert outputs["notifications-ses-domain-verification-token-arn"]
    assert outputs["notifications-ses-configuration-set-arn"]
    for i in range(3):
        assert outputs[f"notifications-dkim-record-{i}-name"]
        assert outputs[f"notifications-dkim-record-{i}-value"]
    assert outputs["notifications-dmarc-record-name"]
    assert outputs["notifications-dmarc-record-value"]


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
    assert outputs["alerts-ses-identity-arn"]
    assert outputs["alerts-ses-domain-verification-token-arn"]
    assert outputs["alerts-ses-configuration-set-arn"]
    for i in range(3):
        assert outputs[f"alerts-dkim-record-{i}-name"]
        assert outputs[f"alerts-dkim-record-{i}-value"]
    assert "alerts-dmarc-record-name" not in outputs


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
    assert outputs["strict-ses-identity-arn"]
    assert outputs["strict-ses-domain-verification-token-arn"]
    assert outputs["strict-ses-configuration-set-arn"]
    for i in range(3):
        assert outputs[f"strict-dkim-record-{i}-name"]
        assert outputs[f"strict-dkim-record-{i}-value"]
    assert outputs["strict-dmarc-record-name"]
    assert outputs["strict-dmarc-record-value"]
