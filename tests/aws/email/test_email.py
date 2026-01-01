from unittest.mock import Mock

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.email import Email, EmailResources
from stelvio.aws.permission import AwsPermission
from stelvio.dns import Dns, DnsProviderNotConfiguredError

from ..pulumi_mocks import PulumiTestMocks

# Test prefix
TP = "test-test-"


class EmailTestMocks(PulumiTestMocks):
    def new_resource(self, args):
        id_, props = super().new_resource(args)

        if args.typ == "aws:sesv2/emailIdentity:EmailIdentity":
            props["dkim_signing_attributes"] = {"tokens": ["token1", "token2", "token3"]}
            props["arn"] = (
                f"arn:aws:ses:us-east-1:123456789012:identity/{args.inputs['emailIdentity']}"
            )

        if args.typ == "aws:sesv2/configurationSet:ConfigurationSet":
            props["arn"] = "arn:aws:ses:us-east-1:123456789012:configuration-set/"
            f"{args.inputs['configurationSetName']}"

        return id_, props


@pytest.fixture
def pulumi_mocks():
    mocks = EmailTestMocks()
    set_mocks(mocks)
    return mocks


@pytest.fixture
def mock_dns():
    dns = Mock(spec=Dns)
    dns.create_record.return_value = Mock()
    return dns


def test_email_initialization_email():
    email = Email("test-email", "test@example.com", dmarc=None)
    assert email.sender == "test@example.com"
    assert email.is_domain is False
    # DMARC is None for email identity
    assert email.dmarc is None


def test_email_initialization_domain(mock_dns):
    email = Email("test-domain", "example.com", dmarc=None, dns=mock_dns)
    assert email.sender == "example.com"
    assert email.is_domain is True
    assert email.dmarc == "v=DMARC1; p=none;"


def test_email_treated_as_domain_if_no_at():
    # "invalid-email" has no @, so it's treated as domain.
    # Since no DNS is configured, it raises DnsProviderNotConfiguredError.
    with pytest.raises(DnsProviderNotConfiguredError):
        Email("test", "invalid-email", dmarc=None)


def test_email_validation_invalid_domain(mock_dns):
    with pytest.raises(ValueError, match="Invalid domain"):
        Email("test", "invalid-domain", dmarc=None, dns=mock_dns)


def test_email_dmarc_validation_no_domain():
    with pytest.raises(ValueError, match="DMARC can only be set for domain email identities"):
        Email("test", "test@example.com", dmarc="v=DMARC1; p=reject;")


def test_email_dmarc_validation_no_dns():
    # Ensure context has no DNS (default fixture behavior)
    with pytest.raises(DnsProviderNotConfiguredError):
        Email("test", "example.com", dmarc="v=DMARC1; p=reject;")


@pulumi.runtime.test
def test_email_resources_creation_email(pulumi_mocks):
    email = Email("test-email", "test@example.com", dmarc=None)

    resources = email.resources

    assert isinstance(resources, EmailResources)

    def check_identity(args):
        identity_email = args
        assert identity_email == "test@example.com"

    return resources.identity.email_identity.apply(check_identity)


@pulumi.runtime.test
def test_email_resources_creation_domain(pulumi_mocks, mock_dns):
    email = Email("test-domain", "example.com", dmarc=None, dns=mock_dns)

    resources = email.resources

    def check_identity(args):
        identity_email = args
        assert identity_email == "example.com"

    # Check that 3 DKIM records were created
    assert len(resources.dkim_records) == 3

    # Check that create_record was called 4 times (3 DKIM + 1 DMARC)
    assert mock_dns.create_record.call_count == 4

    return resources.identity.email_identity.apply(check_identity)


@pulumi.runtime.test
def test_email_link(pulumi_mocks):
    email = Email("test-email", "test@example.com", dmarc=None)
    _ = email.resources

    link = email.link()

    def check_link(args):
        props, perms = args

        # Check properties
        assert props["email_identity_sender"] == "test@example.com"
        assert "email_identity_arn" in props
        assert "identity/test@example.com" in props["email_identity_arn"]

        # Check permissions
        assert len(perms) == 2
        assert all(isinstance(perm, AwsPermission) for perm in perms)

        # First permission: ses:* on identity and config set
        perm1 = perms[0]
        assert perm1.actions == ["ses:*"]
        assert len(perm1.resources) == 2

        # Second permission: sending emails
        perm2 = perms[1]
        expected_actions = ["ses:SendEmail", "ses:SendRawEmail", "ses:SendTemplatedEmail"]
        assert sorted(perm2.actions) == sorted(expected_actions)
        assert len(perm2.resources) == 1

    # For properties:
    props_output = pulumi.Output.all(**link.properties)

    # For permissions:
    return props_output.apply(lambda props: check_link((props, link.permissions)))
