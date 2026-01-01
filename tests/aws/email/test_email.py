"""Tests for the Email component."""

from pathlib import Path

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.email import Email, EmailConfig, EmailConfigDict, Event
from stelvio.aws.function import Function

from ...test_utils import assert_config_dict_matches_dataclass
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, PulumiTestMocks


def delete_files(directory: Path, filename: str):
    directory_path = directory
    for file_path in directory_path.rglob(filename):
        file_path.unlink()


@pytest.fixture(autouse=True)
def project_cwd(monkeypatch, pytestconfig):
    rootpath = pytestconfig.rootpath
    test_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    monkeypatch.chdir(test_project_dir)
    yield test_project_dir
    delete_files(test_project_dir, "stlv_resources.py")


# Test prefix
TP = "test-test-"


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


# ============================================================================
# Configuration Tests (no mocks needed)
# ============================================================================


def test_config_dict_matches_dataclass():
    """Test that EmailConfigDict matches EmailConfig."""
    assert_config_dict_matches_dataclass(EmailConfig, EmailConfigDict)


def test_email_config_requires_sender():
    """Test that sender is required."""
    with pytest.raises(ValueError, match="sender is required"):
        EmailConfig(sender="")


def test_email_config_dmarc_only_for_domain():
    """Test that DMARC can only be set for domain senders."""
    with pytest.raises(ValueError, match="DMARC can only be set for domain senders"):
        EmailConfig(sender="user@example.com", dmarc="v=DMARC1; p=none;")


def test_email_config_is_email():
    """Test is_email property."""
    email_config = EmailConfig(sender="user@example.com")
    assert email_config.is_email is True
    assert email_config.is_domain is False

    domain_config = EmailConfig(sender="example.com")
    assert domain_config.is_email is False
    assert domain_config.is_domain is True


def test_event_requires_topic_or_bus():
    """Test that Event requires either topic or bus."""
    with pytest.raises(ValueError, match="must have either 'topic' or 'bus' specified"):
        Event(name="test", types=["bounce"])


def test_event_cannot_have_both_topic_and_bus():
    """Test that Event cannot have both topic and bus."""
    with pytest.raises(ValueError, match="cannot have both 'topic' and 'bus' specified"):
        Event(
            name="test",
            types=["bounce"],
            topic="arn:aws:sns:us-east-1:123456789:topic",
            bus="arn:aws:events:us-east-1:123456789:event-bus/default",
        )


def test_event_requires_types():
    """Test that Event requires at least one event type."""
    with pytest.raises(ValueError, match="must have at least one event type"):
        Event(name="test", types=[], topic="arn:aws:sns:us-east-1:123456789:topic")


def test_email_invalid_config_combination():
    """Test that combining config parameter with options raises ValueError."""
    config = EmailConfig(sender="user@example.com")
    with pytest.raises(
        ValueError, match="cannot combine 'config' parameter with additional options"
    ):
        Email("test", config=config, dmarc="v=DMARC1; p=none;")


def test_email_invalid_config_combination_with_sender():
    """Test that combining config with sender raises ValueError."""
    config = EmailConfig(sender="user@example.com")
    with pytest.raises(
        ValueError, match="cannot combine 'config' parameter with additional options"
    ):
        Email("test", "other@example.com", config=config)


def test_email_requires_sender():
    """Test that Email requires sender."""
    with pytest.raises(ValueError, match="sender is required"):
        Email("test")


# ============================================================================
# Resource Creation Tests
# ============================================================================


@pulumi.runtime.test
def test_creates_email_identity_for_email_address(pulumi_mocks):
    """Test creating Email with email address sender."""
    email = Email("my-email", "user@example.com")
    _ = email.resources

    def check_resources(_):
        identities = pulumi_mocks.created_ses_identities()
        assert len(identities) == 1
        assert identities[0].inputs["emailIdentity"] == "user@example.com"

        config_sets = pulumi_mocks.created_ses_configuration_sets()
        assert len(config_sets) == 1

        # No domain verification for email address
        verifications = pulumi_mocks.created_ses_domain_verifications()
        assert len(verifications) == 0

    email.resources.identity.id.apply(check_resources)


@pulumi.runtime.test
def test_creates_email_identity_for_domain(pulumi_mocks):
    """Test creating Email with domain sender."""
    email = Email("my-email", "example.com")
    _ = email.resources

    def check_resources(_):
        identities = pulumi_mocks.created_ses_identities()
        assert len(identities) == 1
        assert identities[0].inputs["emailIdentity"] == "example.com"

        config_sets = pulumi_mocks.created_ses_configuration_sets()
        assert len(config_sets) == 1

        # Domain verification should be created for domain senders
        verifications = pulumi_mocks.created_ses_domain_verifications()
        assert len(verifications) == 1
        assert verifications[0].inputs["domain"] == "example.com"

    # Wait for domain verification resource to be created
    email.resources.domain_verification.id.apply(check_resources)


@pulumi.runtime.test
def test_creates_configuration_set_linked_to_identity(pulumi_mocks):
    """Test that identity uses the configuration set."""
    email = Email("my-email", "user@example.com")
    _ = email.resources

    def check_resources(_):
        identities = pulumi_mocks.created_ses_identities()
        config_sets = pulumi_mocks.created_ses_configuration_sets()

        assert len(identities) == 1
        assert len(config_sets) == 1

        # Identity should reference configuration set
        assert "configurationSetName" in identities[0].inputs

    email.resources.identity.id.apply(check_resources)


@pulumi.runtime.test
def test_creates_event_destination_with_sns_topic(pulumi_mocks):
    """Test creating Email with SNS topic event destination."""
    topic_arn = "arn:aws:sns:us-east-1:123456789012:my-topic"
    email = Email(
        "my-email",
        "user@example.com",
        events=[Event(name="bounces", types=["bounce", "complaint"], topic=topic_arn)],
    )
    _ = email.resources

    def check_resources(_):
        event_destinations = pulumi_mocks.created_ses_event_destinations()
        assert len(event_destinations) == 1

        dest = event_destinations[0]
        assert dest.inputs["eventDestinationName"] == "bounces"
        assert dest.inputs["eventDestination"]["matchingEventTypes"] == ["BOUNCE", "COMPLAINT"]
        assert dest.inputs["eventDestination"]["snsDestination"]["topicArn"] == topic_arn
        assert dest.inputs["eventDestination"]["enabled"] is True

    email.resources.identity.id.apply(check_resources)


@pulumi.runtime.test
def test_creates_event_destination_with_eventbridge(pulumi_mocks):
    """Test creating Email with EventBridge event destination."""
    bus_arn = "arn:aws:events:us-east-1:123456789012:event-bus/default"
    email = Email(
        "my-email",
        "user@example.com",
        events=[Event(name="deliveries", types=["delivery"], bus=bus_arn)],
    )
    _ = email.resources

    def check_resources(_):
        event_destinations = pulumi_mocks.created_ses_event_destinations()
        assert len(event_destinations) == 1

        dest = event_destinations[0]
        assert dest.inputs["eventDestinationName"] == "deliveries"
        assert dest.inputs["eventDestination"]["matchingEventTypes"] == ["DELIVERY"]
        assert dest.inputs["eventDestination"]["eventBridgeDestination"]["eventBusArn"] == bus_arn

    email.resources.identity.id.apply(check_resources)


@pulumi.runtime.test
def test_creates_multiple_event_destinations(pulumi_mocks):
    """Test creating Email with multiple event destinations."""
    email = Email(
        "my-email",
        "user@example.com",
        events=[
            Event(
                name="bounces",
                types=["bounce"],
                topic="arn:aws:sns:us-east-1:123456789012:bounces",
            ),
            Event(
                name="deliveries",
                types=["delivery", "send"],
                topic="arn:aws:sns:us-east-1:123456789012:deliveries",
            ),
        ],
    )
    _ = email.resources

    def check_resources(_):
        event_destinations = pulumi_mocks.created_ses_event_destinations()
        assert len(event_destinations) == 2

        dest_names = {d.inputs["eventDestinationName"] for d in event_destinations}
        assert dest_names == {"bounces", "deliveries"}

    email.resources.identity.id.apply(check_resources)


@pulumi.runtime.test
def test_event_type_conversion(pulumi_mocks):
    """Test that event types are converted to uppercase with underscores."""
    email = Email(
        "my-email",
        "user@example.com",
        events=[
            Event(
                name="test",
                types=["delivery-delay", "rendering-failure"],
                topic="arn:aws:sns:us-east-1:123456789012:topic",
            )
        ],
    )
    _ = email.resources

    def check_resources(_):
        event_destinations = pulumi_mocks.created_ses_event_destinations()
        assert len(event_destinations) == 1

        dest = event_destinations[0]
        assert dest.inputs["eventDestination"]["matchingEventTypes"] == [
            "DELIVERY_DELAY",
            "RENDERING_FAILURE",
        ]

    email.resources.identity.id.apply(check_resources)


@pulumi.runtime.test
def test_email_with_config_dict(pulumi_mocks):
    """Test creating Email with dict config."""
    email = Email(
        "my-email",
        config={"sender": "user@example.com"},
    )
    _ = email.resources

    def check_resources(_):
        identities = pulumi_mocks.created_ses_identities()
        assert len(identities) == 1
        assert identities[0].inputs["emailIdentity"] == "user@example.com"

    email.resources.identity.id.apply(check_resources)


@pulumi.runtime.test
def test_email_with_config_object(pulumi_mocks):
    """Test creating Email with EmailConfig object."""
    config = EmailConfig(sender="domain.com")
    email = Email("my-email", config=config)
    _ = email.resources

    def check_resources(_):
        identities = pulumi_mocks.created_ses_identities()
        assert len(identities) == 1
        assert identities[0].inputs["emailIdentity"] == "domain.com"

    email.resources.identity.id.apply(check_resources)


@pulumi.runtime.test
def test_email_with_event_dict(pulumi_mocks):
    """Test creating Email with EventDict."""
    email = Email(
        "my-email",
        "user@example.com",
        events=[{"name": "test", "types": ["bounce"], "topic": "arn:aws:sns:us-east-1:123:topic"}],
    )
    _ = email.resources

    def check_resources(_):
        event_destinations = pulumi_mocks.created_ses_event_destinations()
        assert len(event_destinations) == 1

    email.resources.identity.id.apply(check_resources)


# ============================================================================
# Properties Tests
# ============================================================================


@pulumi.runtime.test
def test_sender_property(pulumi_mocks):
    """Test sender property returns correct value."""
    email = Email("my-email", "user@example.com")
    assert email.sender == "user@example.com"


@pulumi.runtime.test
def test_identity_arn_property(pulumi_mocks):
    """Test identity_arn property."""
    email = Email("my-email", "user@example.com")
    _ = email.resources

    def check_arn(arn):
        assert f"arn:aws:ses:{DEFAULT_REGION}:{ACCOUNT_ID}:identity/user@example.com" == arn

    email.identity_arn.apply(check_arn)


# ============================================================================
# Linking Tests
# ============================================================================


@pulumi.runtime.test
def test_email_link(pulumi_mocks):
    """Test Email link method."""
    email = Email("my-email", "user@example.com")
    link = email.link()

    assert link.name == "my-email"

    def check_link(args):
        properties, permissions = args

        # Check properties
        assert "sender" in properties
        assert "config_set" in properties
        assert properties["sender"] == "user@example.com"

        # Check permissions
        assert len(permissions) == 2

        # First permission: ses:* on identity and config set
        ses_wildcard = permissions[0]
        assert "ses:*" in ses_wildcard.actions
        assert len(ses_wildcard.resources) == 2

        # Second permission: send email operations on wildcard
        send_permission = permissions[1]
        assert "ses:SendEmail" in send_permission.actions
        assert "ses:SendRawEmail" in send_permission.actions
        assert "ses:SendTemplatedEmail" in send_permission.actions
        assert send_permission.resources == ["*"]

    pulumi.Output.all(link.properties, link.permissions).apply(check_link)


@pulumi.runtime.test
def test_email_linkable_with_function(pulumi_mocks):
    """Test that Email can be linked to a Function."""
    email = Email("my-email", "user@example.com")

    function = Function(
        "email-sender",
        handler="functions/simple.handler",
        links=[email],
    )

    def check_function(_):
        # Function should have been created with the email link
        functions = pulumi_mocks.created_functions()
        assert len(functions) == 1

        # Check the policy includes SES permissions
        policies = pulumi_mocks.created_policies()
        assert len(policies) >= 1

    function.resources.function.id.apply(check_function)


# ============================================================================
# Resources Dataclass Tests
# ============================================================================


def test_email_resources_dataclass():
    """Test EmailResources is a frozen dataclass."""
    import dataclasses

    from stelvio.aws.email import EmailResources

    assert dataclasses.is_dataclass(EmailResources)
    assert EmailResources.__dataclass_params__.frozen
