from unittest.mock import Mock

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.email import Email, EmailConfig, EmailConfigDict, EmailResources
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
            props["arn"] = (
                f"arn:aws:ses:us-east-1:123456789012:configuration-set/"
                f"{args.inputs['configurationSetName']}"
            )

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
        assert len(perms) == 1
        assert all(isinstance(perm, AwsPermission) for perm in perms)

        # Permission: sending emails
        perm = perms[0]
        expected_actions = ["ses:SendEmail", "ses:SendRawEmail", "ses:SendTemplatedEmail"]
        assert sorted(perm.actions) == sorted(expected_actions)
        assert len(perm.resources) == 1

    # For properties:
    props_output = pulumi.Output.all(**link.properties)

    # For permissions:
    return props_output.apply(lambda props: check_link((props, link.permissions)))


# ============================================================================
# Configuration Tests
# ============================================================================


def test_email_with_typed_config():
    """Test Email initialization with EmailConfig typed config."""
    config = EmailConfig(sender="typed@example.com", dmarc=None)
    email = Email("test-typed", config)
    assert email.sender == "typed@example.com"
    assert email.dmarc is None
    assert email.is_domain is False


def test_email_with_dict_config():
    """Test Email initialization with EmailConfigDict dictionary config."""
    config: EmailConfigDict = {"sender": "dict@example.com", "dmarc": None}
    email = Email("test-dict", config)
    assert email.sender == "dict@example.com"
    assert email.dmarc is None


def test_email_with_string_config():
    """Test Email initialization with string shorthand for sender."""
    email = Email("test-string", "string@example.com", dmarc=None)
    assert email.sender == "string@example.com"


# ============================================================================
# Sandbox Mode Tests
# ============================================================================


def test_email_sandbox_mode_initialization():
    """Test Email initialization with sandbox mode enabled."""
    email = Email("test-sandbox", "sandbox@example.com", dmarc=None, sandbox=True)
    assert email.sandbox is True
    assert email.sender == "sandbox@example.com"


def test_email_sandbox_mode_default_false():
    """Test that sandbox mode defaults to False."""
    email = Email("test-default", "test@example.com", dmarc=None)
    assert email.sandbox is False


@pulumi.runtime.test
def test_email_link_sandbox_mode(pulumi_mocks):
    """Test that sandbox mode uses '*' for send email resources."""
    email = Email("test-sandbox", "sandbox@example.com", dmarc=None, sandbox=True)
    _ = email.resources

    link = email.link()

    def check_link(args):
        props, perms = args

        # Check that sandbox email has correct permissions
        assert len(perms) == 1

        # Permission should use "*" for sandbox mode
        perm = perms[0]
        expected_actions = ["ses:SendEmail", "ses:SendRawEmail", "ses:SendTemplatedEmail"]
        assert sorted(perm.actions) == sorted(expected_actions)
        # In sandbox mode, resource should be "*"
        assert perm.resources == ["*"]

    props_output = pulumi.Output.all(**link.properties)
    return props_output.apply(lambda props: check_link((props, link.permissions)))


# ============================================================================
# DMARC Tests
# ============================================================================


def test_email_domain_default_dmarc(mock_dns):
    """Test that domain identity gets default DMARC when dmarc=None."""
    email = Email("test-domain", "example.com", dmarc=None, dns=mock_dns)
    assert email.dmarc == "v=DMARC1; p=none;"


def test_email_domain_custom_dmarc(mock_dns):
    """Test domain identity with custom DMARC policy."""
    custom_dmarc = "v=DMARC1; p=reject; rua=mailto:dmarc@example.com"
    email = Email("test-domain", "example.com", dmarc=custom_dmarc, dns=mock_dns)
    assert email.dmarc == custom_dmarc


def test_email_domain_dmarc_false_disables_dmarc(mock_dns):
    """Test that dmarc=False explicitly disables DMARC for domain."""
    email = Email("test-domain", "example.com", dmarc=False, dns=mock_dns)
    assert email.dmarc is None


@pulumi.runtime.test
def test_email_domain_dmarc_false_no_record(pulumi_mocks, mock_dns):
    """Test that dmarc=False does not create DMARC record."""
    email = Email("test-domain", "example.com", dmarc=False, dns=mock_dns)
    resources = email.resources

    # Should create 3 DKIM records but no DMARC record
    assert len(resources.dkim_records) == 3
    assert resources.dmarc_record is None
    # Only 3 DKIM calls, no DMARC call
    assert mock_dns.create_record.call_count == 3


# ============================================================================
# Events Configuration Tests
# ============================================================================


def test_email_with_events_config():
    """Test Email initialization with events configuration."""
    events = [
        {
            "name": "bounce-handler",
            "types": ["bounce", "complaint"],
            "topic_arn": "arn:aws:sns:us-east-1:123456789012:my-topic",
        }
    ]
    email = Email("test-events", "events@example.com", dmarc=None, events=events)
    assert email.events == events
    assert len(email.events) == 1
    assert email.events[0]["name"] == "bounce-handler"


def test_email_events_default_none():
    """Test that events defaults to None."""
    email = Email("test-no-events", "test@example.com", dmarc=None)
    assert email.events is None


@pulumi.runtime.test
def test_email_event_destinations_creation(pulumi_mocks):
    """Test that event destinations are created when events are configured."""
    events = [
        {
            "name": "bounce-handler",
            "types": ["bounce", "complaint"],
            "topic_arn": "arn:aws:sns:us-east-1:123456789012:bounce-topic",
        },
        {
            "name": "delivery-tracker",
            "types": ["delivery", "send"],
            "topic_arn": "arn:aws:sns:us-east-1:123456789012:delivery-topic",
        },
    ]
    email = Email("test-events", "events@example.com", dmarc=None, events=events)
    resources = email.resources

    assert resources.event_destinations is not None
    assert len(resources.event_destinations) == 2


# ============================================================================
# Validation Tests
# ============================================================================


def test_email_validation_invalid_email_no_at():
    """Test that string without @ is treated as domain and requires DNS."""
    # Without @, it's treated as a domain which requires DNS
    with pytest.raises(DnsProviderNotConfiguredError):
        Email("test", "notanemail", dmarc=None)


def test_email_validation_invalid_email_format(mock_dns):
    """Test that invalid domain format raises error."""
    # "nodot" has no dot, so it's an invalid domain
    with pytest.raises(ValueError, match="Invalid domain"):
        Email("test", "nodot", dmarc=None, dns=mock_dns)


def test_email_domain_requires_dns():
    """Test that domain identity requires DNS provider."""
    with pytest.raises(DnsProviderNotConfiguredError):
        Email("test", "example.com", dmarc=None)


def test_email_dmarc_requires_dns_for_domain():
    """Test that DMARC with domain requires DNS provider."""
    with pytest.raises(DnsProviderNotConfiguredError):
        Email("test", "example.com", dmarc="v=DMARC1; p=reject;")


# ============================================================================
# Resource Creation Tests
# ============================================================================


@pulumi.runtime.test
def test_email_resources_has_configuration_set(pulumi_mocks):
    """Test that email resources include configuration set."""
    email = Email("test-email", "test@example.com", dmarc=None)
    resources = email.resources

    assert resources.configuration_set is not None

    def check_config_set(name):
        assert "config-set" in name

    return resources.configuration_set.configuration_set_name.apply(check_config_set)


@pulumi.runtime.test
def test_email_resources_domain_has_verification(pulumi_mocks, mock_dns):
    """Test that domain identity has verification resource."""
    email = Email("test-domain", "example.com", dmarc=None, dns=mock_dns)
    resources = email.resources

    assert resources.verification is not None
    assert resources.dkim_records is not None
    assert len(resources.dkim_records) == 3


@pulumi.runtime.test
def test_email_resources_email_no_dkim(pulumi_mocks):
    """Test that email identity does not have DKIM records."""
    email = Email("test-email", "test@example.com", dmarc=None)
    resources = email.resources

    assert resources.dkim_records is None
    assert resources.dmarc_record is None
    assert resources.verification is None


# ============================================================================
# Link Configuration Tests
# ============================================================================


@pulumi.runtime.test
def test_email_link_has_all_properties(pulumi_mocks):
    """Test that link exposes all required properties."""
    email = Email("test-email", "test@example.com", dmarc=None)
    _ = email.resources

    link = email.link()

    def check_properties(props):
        assert "email_identity_sender" in props
        assert "email_identity_arn" in props
        assert "configuration_set_name" in props
        assert "configuration_set_arn" in props

    return pulumi.Output.all(**link.properties).apply(check_properties)


@pulumi.runtime.test
def test_email_link_configuration_set_properties(pulumi_mocks):
    """Test that link includes correct configuration set values."""
    email = Email("test-config-set", "config@example.com", dmarc=None)
    _ = email.resources

    link = email.link()

    def check_config_set_props(props):
        # Verify configuration set name contains expected pattern
        assert "config-set" in props["configuration_set_name"]
        # Verify configuration set ARN is present and valid
        assert props["configuration_set_arn"] is not None
        assert "configuration-set" in props["configuration_set_arn"]

    return pulumi.Output.all(**link.properties).apply(check_config_set_props)


@pulumi.runtime.test
def test_email_link_non_sandbox_uses_identity_arn(pulumi_mocks):
    """Test that non-sandbox mode uses identity ARN for send permissions."""
    email = Email("test-non-sandbox", "sender@example.com", dmarc=None, sandbox=False)
    _ = email.resources

    link = email.link()

    def check_permissions(args):
        props, perms = args

        assert len(perms) == 1
        perm = perms[0]

        # Non-sandbox should use identity ARN, not "*"
        assert len(perm.resources) == 1
        # The resource should be an Output containing the identity ARN
        assert perm.resources != ["*"]

    props_output = pulumi.Output.all(**link.properties)
    return props_output.apply(lambda props: check_permissions((props, link.permissions)))


@pulumi.runtime.test
def test_email_link_sandbox_vs_non_sandbox_difference(pulumi_mocks):
    """Test the IAM permission difference between sandbox and non-sandbox mode."""
    # Create non-sandbox email
    email_normal = Email("test-normal", "normal@example.com", dmarc=None, sandbox=False)
    _ = email_normal.resources
    link_normal = email_normal.link()

    # Create sandbox email
    email_sandbox = Email("test-sandbox-diff", "sandbox@example.com", dmarc=None, sandbox=True)
    _ = email_sandbox.resources
    link_sandbox = email_sandbox.link()

    # Non-sandbox should have identity ARN as resource (Output type)
    normal_perm = link_normal.permissions[0]
    sandbox_perm = link_sandbox.permissions[0]

    # Sandbox uses literal "*", non-sandbox uses Output (identity ARN)
    assert sandbox_perm.resources == ["*"]
    # Normal permission resource is not a literal "*"
    assert normal_perm.resources != ["*"]


# ============================================================================
# Event Destination Resource Tests
# ============================================================================


@pulumi.runtime.test
def test_email_event_destination_properties(pulumi_mocks):
    """Test that event destination resources have correct properties."""
    events = [
        {
            "name": "bounce-handler",
            "types": ["bounce", "complaint"],
            "topic_arn": "arn:aws:sns:us-east-1:123456789012:bounce-topic",
        },
    ]
    email = Email("test-event-props", "events@example.com", dmarc=None, events=events)
    resources = email.resources

    assert resources.event_destinations is not None
    assert len(resources.event_destinations) == 1

    event_dest = resources.event_destinations[0]

    def check_event_dest(name):
        assert "bounce-handler" in name

    return event_dest.event_destination_name.apply(check_event_dest)


@pulumi.runtime.test
def test_email_multiple_event_destinations(pulumi_mocks):
    """Test creating multiple event destinations."""
    events = [
        {
            "name": "bounce-events",
            "types": ["bounce"],
            "topic_arn": "arn:aws:sns:us-east-1:123456789012:bounce-topic",
        },
        {
            "name": "delivery-events",
            "types": ["delivery", "send"],
            "topic_arn": "arn:aws:sns:us-east-1:123456789012:delivery-topic",
        },
        {
            "name": "complaint-events",
            "types": ["complaint"],
            "topic_arn": "arn:aws:sns:us-east-1:123456789012:complaint-topic",
        },
    ]
    email = Email("test-multi-events", "multi@example.com", dmarc=None, events=events)
    resources = email.resources

    assert resources.event_destinations is not None
    assert len(resources.event_destinations) == 3


@pulumi.runtime.test
def test_email_no_events_empty_list(pulumi_mocks):
    """Test that no events results in empty event destinations list."""
    email = Email("test-no-events-list", "noevent@example.com", dmarc=None)
    resources = email.resources

    # Should be empty list, not None
    assert resources.event_destinations == []


# ============================================================================
# DMARC Explicit Disable Tests
# ============================================================================


@pulumi.runtime.test
def test_email_dmarc_false_with_domain_no_dmarc_dns_call(pulumi_mocks, mock_dns):
    """Test that dmarc=False prevents DMARC DNS record creation for domain."""
    email = Email("test-no-dmarc", "nodmarc.example.com", dmarc=False, dns=mock_dns)
    resources = email.resources

    # Should have DKIM records but no DMARC record
    assert resources.dkim_records is not None
    assert len(resources.dkim_records) == 3
    assert resources.dmarc_record is None

    # DNS should only be called for DKIM (3 times), not for DMARC
    # Total calls: 3 DKIM records
    assert mock_dns.create_record.call_count == 3


def test_email_dmarc_false_vs_none_for_domain(mock_dns):
    """Test difference between dmarc=False and dmarc=None for domain identity."""
    # dmarc=None should get default DMARC
    email_default = Email("test-default-dmarc", "default.example.com", dmarc=None, dns=mock_dns)
    assert email_default.dmarc == "v=DMARC1; p=none;"

    # dmarc=False should explicitly disable DMARC
    email_disabled = Email(
        "test-disabled-dmarc", "disabled.example.com", dmarc=False, dns=mock_dns
    )
    assert email_disabled.dmarc is None


def test_email_dmarc_none_for_email_identity():
    """Test that email identity (not domain) has None dmarc regardless of input."""
    email = Email("test-email-dmarc", "user@example.com", dmarc=None)
    # Email identities don't have DMARC
    assert email.dmarc is None

