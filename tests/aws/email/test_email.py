"""Tests for the Email component."""

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.email import (
    Email,
    EmailConfig,
    EmailResources,
    EventDestination,
)
from stelvio.aws.permission import AwsPermission
from stelvio.component import ComponentRegistry
from stelvio.link import Link

from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, PulumiTestMocks, MockDns, tn


# Test prefix (matches the test context prefix)
TP = "test-test-"


@pytest.fixture
def pulumi_mocks():
    """Set up Pulumi mocks for testing."""
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


@pytest.fixture
def mock_dns():
    """Create a mock DNS provider."""
    return MockDns()


class TestEmailConfig:
    """Tests for EmailConfig dataclass."""

    def test_email_config_with_email_address(self):
        """Test creating config with an email address sender."""
        config = EmailConfig(sender="test@example.com")

        assert config.sender == "test@example.com"
        assert config.is_domain is False
        assert config.dns is None
        assert config.dmarc is None
        assert config.events == []

    def test_email_config_with_domain(self):
        """Test creating config with a domain sender."""
        config = EmailConfig(sender="example.com")

        assert config.sender == "example.com"
        assert config.is_domain is True

    def test_email_config_with_custom_dmarc(self):
        """Test creating config with custom DMARC policy."""
        config = EmailConfig(
            sender="example.com",
            dmarc="v=DMARC1; p=quarantine; adkim=s; aspf=s;"
        )

        assert config.dmarc == "v=DMARC1; p=quarantine; adkim=s; aspf=s;"
        assert config.normalized_dmarc == "v=DMARC1; p=quarantine; adkim=s; aspf=s;"

    def test_email_config_default_dmarc(self):
        """Test default DMARC policy."""
        config = EmailConfig(sender="example.com")

        assert config.dmarc is None
        assert config.normalized_dmarc == "v=DMARC1; p=none;"

    def test_email_config_with_events(self):
        """Test creating config with event destinations."""
        event = EventDestination(
            name="bounce-handler",
            types=["bounce", "complaint"],
            topic_arn="arn:aws:sns:us-east-1:123456789012:MyTopic"
        )
        config = EmailConfig(sender="example.com", events=[event])

        assert len(config.events) == 1
        assert config.events[0].name == "bounce-handler"
        assert config.events[0].types == ["bounce", "complaint"]

    def test_email_config_dns_only_valid_for_domain(self, mock_dns):
        """Test that dns parameter is rejected for email address senders."""
        with pytest.raises(ValueError, match="'dns' is only valid when 'sender' is a domain"):
            EmailConfig(sender="test@example.com", dns=mock_dns)

    def test_email_config_dmarc_only_valid_for_domain(self):
        """Test that dmarc parameter is rejected for email address senders."""
        with pytest.raises(ValueError, match="'dmarc' is only valid when 'sender' is a domain"):
            EmailConfig(sender="test@example.com", dmarc="v=DMARC1; p=none;")

    def test_email_config_requires_sender(self):
        """Test that sender is required."""
        with pytest.raises(ValueError, match="sender is required"):
            EmailConfig(sender="")


class TestEventDestination:
    """Tests for EventDestination dataclass."""

    def test_event_destination_with_topic(self):
        """Test creating event destination with SNS topic."""
        event = EventDestination(
            name="notifications",
            types=["delivery", "bounce"],
            topic_arn="arn:aws:sns:us-east-1:123456789012:MyTopic"
        )

        assert event.name == "notifications"
        assert event.types == ["delivery", "bounce"]
        assert event.topic_arn == "arn:aws:sns:us-east-1:123456789012:MyTopic"
        assert event.bus_arn is None

    def test_event_destination_with_eventbridge(self):
        """Test creating event destination with EventBridge bus."""
        event = EventDestination(
            name="analytics",
            types=["send", "open", "click"],
            bus_arn="arn:aws:events:us-east-1:123456789012:event-bus/MyBus"
        )

        assert event.name == "analytics"
        assert event.bus_arn == "arn:aws:events:us-east-1:123456789012:event-bus/MyBus"
        assert event.topic_arn is None

    def test_event_destination_requires_destination(self):
        """Test that either topic_arn or bus_arn is required."""
        with pytest.raises(ValueError, match="requires either 'topic_arn' or 'bus_arn'"):
            EventDestination(name="invalid", types=["bounce"])

    def test_event_destination_cannot_have_both_destinations(self):
        """Test that both topic_arn and bus_arn cannot be provided."""
        with pytest.raises(ValueError, match="cannot have both 'topic_arn' and 'bus_arn'"):
            EventDestination(
                name="invalid",
                types=["bounce"],
                topic_arn="arn:aws:sns:us-east-1:123456789012:MyTopic",
                bus_arn="arn:aws:events:us-east-1:123456789012:event-bus/MyBus"
            )


class TestEmailComponentCreation:
    """Tests for Email component creation."""

    @pulumi.runtime.test
    def test_create_email_with_email_address(self, pulumi_mocks):
        """Test creating email component with an email address."""
        email = Email("notifications", sender="alerts@example.com")

        # Trigger resource creation
        _ = email.resources

        # Verify resources were created using async check
        def check_resources(_):
            identities = pulumi_mocks.created_ses_email_identities()
            assert len(identities) == 1
            assert identities[0].inputs["emailIdentity"] == "alerts@example.com"

            config_sets = pulumi_mocks.created_ses_configuration_sets()
            assert len(config_sets) == 1

            # No domain verification for email address
            verifications = pulumi_mocks.created_ses_domain_verifications()
            assert len(verifications) == 0

        email.resources.identity.arn.apply(check_resources)

    @pulumi.runtime.test
    def test_create_email_with_domain_no_dns(self, pulumi_mocks):
        """Test creating email component with a domain but manual DNS (dns=False)."""
        email = Email("marketing", sender="example.com", dns=False)

        # Trigger resource creation
        _ = email.resources

        def check_resources(_):
            identities = pulumi_mocks.created_ses_email_identities()
            assert len(identities) == 1
            assert identities[0].inputs["emailIdentity"] == "example.com"

            # Domain verification should still be created
            verifications = pulumi_mocks.created_ses_domain_verifications()
            assert len(verifications) == 1
            assert verifications[0].inputs["domain"] == "example.com"

        # Wait on domain_verification since it's created last
        email.resources.domain_verification.arn.apply(check_resources)

    @pulumi.runtime.test
    def test_create_email_with_config_object(self, pulumi_mocks):
        """Test creating email with EmailConfig object."""
        config = EmailConfig(sender="test@example.com")
        email = Email("test", config=config)

        _ = email.resources

        def check_resources(_):
            identities = pulumi_mocks.created_ses_email_identities()
            assert len(identities) == 1
            assert identities[0].inputs["emailIdentity"] == "test@example.com"

        email.resources.identity.arn.apply(check_resources)

    @pulumi.runtime.test
    def test_create_email_with_dict_config(self, pulumi_mocks):
        """Test creating email with dictionary config."""
        email = Email("test", config={"sender": "test@example.com"})

        _ = email.resources

        def check_resources(_):
            identities = pulumi_mocks.created_ses_email_identities()
            assert len(identities) == 1

        email.resources.identity.arn.apply(check_resources)

    def test_create_email_rejects_both_config_and_opts(self, pulumi_mocks):
        """Test that providing both config and opts raises an error."""
        config = EmailConfig(sender="test@example.com")

        with pytest.raises(ValueError, match="Cannot provide both 'config' and individual parameters"):
            Email("test", config=config, sender="other@example.com")

    def test_create_email_requires_parameters(self, pulumi_mocks):
        """Test that email requires at least a sender."""
        with pytest.raises(ValueError, match="Email requires at least a 'sender' parameter"):
            Email("test")


class TestEmailWithEvents:
    """Tests for Email component with event destinations."""

    @pulumi.runtime.test
    def test_create_email_with_sns_event_destination(self, pulumi_mocks):
        """Test creating email with SNS event destination."""
        email = Email(
            "notifications",
            sender="alerts@example.com",
            events=[
                EventDestination(
                    name="bounces",
                    types=["bounce", "complaint"],
                    topic_arn="arn:aws:sns:us-east-1:123456789012:BounceNotifications"
                )
            ]
        )

        _ = email.resources

        def check_resources(_):
            event_destinations = pulumi_mocks.created_ses_event_destinations()
            assert len(event_destinations) == 1
            assert event_destinations[0].inputs["eventDestinationName"] == "bounces"

            event_dest_config = event_destinations[0].inputs["eventDestination"]
            assert "BOUNCE" in event_dest_config["matchingEventTypes"]
            assert "COMPLAINT" in event_dest_config["matchingEventTypes"]
            assert event_dest_config["enabled"] is True
            assert "snsDestination" in event_dest_config

        # Wait on event destination to ensure it's created
        email.resources.event_destinations[0].id.apply(check_resources)

    @pulumi.runtime.test
    def test_create_email_with_eventbridge_destination(self, pulumi_mocks):
        """Test creating email with EventBridge event destination."""
        email = Email(
            "analytics",
            sender="analytics@example.com",
            events=[
                EventDestination(
                    name="tracking",
                    types=["send", "open", "click"],
                    bus_arn="arn:aws:events:us-east-1:123456789012:event-bus/Analytics"
                )
            ]
        )

        _ = email.resources

        def check_resources(_):
            event_destinations = pulumi_mocks.created_ses_event_destinations()
            assert len(event_destinations) == 1

            event_dest_config = event_destinations[0].inputs["eventDestination"]
            assert "SEND" in event_dest_config["matchingEventTypes"]
            assert "OPEN" in event_dest_config["matchingEventTypes"]
            assert "CLICK" in event_dest_config["matchingEventTypes"]
            assert "eventBridgeDestination" in event_dest_config

        # Wait on event destination to ensure it's created
        email.resources.event_destinations[0].id.apply(check_resources)

    @pulumi.runtime.test
    def test_create_email_with_multiple_event_destinations(self, pulumi_mocks):
        """Test creating email with multiple event destinations."""
        email = Email(
            "multi-events",
            sender="test@example.com",
            events=[
                EventDestination(
                    name="errors",
                    types=["bounce", "reject"],
                    topic_arn="arn:aws:sns:us-east-1:123456789012:Errors"
                ),
                EventDestination(
                    name="analytics",
                    types=["delivery", "open", "click"],
                    bus_arn="arn:aws:events:us-east-1:123456789012:event-bus/Analytics"
                )
            ]
        )

        _ = email.resources

        def check_resources(_):
            event_destinations = pulumi_mocks.created_ses_event_destinations()
            assert len(event_destinations) == 2

        email.resources.identity.arn.apply(check_resources)


class TestEmailLink:
    """Tests for Email component linking functionality."""

    @pulumi.runtime.test
    def test_email_link_returns_link_object(self, pulumi_mocks):
        """Test that email.link() returns a Link object."""
        email = Email("test", sender="test@example.com")
        _ = email.resources

        link = email.link()

        assert isinstance(link, Link)
        assert link.name == "test"

    @pulumi.runtime.test
    def test_email_link_has_properties(self, pulumi_mocks):
        """Test that email link has correct properties."""
        email = Email("test", sender="test@example.com")
        _ = email.resources

        link = email.link()

        assert link.properties is not None
        assert "sender" in link.properties
        assert "config_set" in link.properties

    @pulumi.runtime.test
    def test_email_link_has_ses_permissions(self, pulumi_mocks):
        """Test that email link has SES permissions."""
        email = Email("test", sender="test@example.com")
        _ = email.resources

        link = email.link()

        assert link.permissions is not None
        assert len(link.permissions) == 2

        # Check first permission (full SES access to identity and config set)
        full_access_permission = link.permissions[0]
        assert isinstance(full_access_permission, AwsPermission)
        assert "ses:*" in full_access_permission.actions

        # Check second permission (send permissions)
        send_permission = link.permissions[1]
        assert "ses:SendEmail" in send_permission.actions
        assert "ses:SendRawEmail" in send_permission.actions
        assert "ses:SendTemplatedEmail" in send_permission.actions


class TestEmailProperties:
    """Tests for Email component properties."""

    @pulumi.runtime.test
    def test_email_config_property(self, pulumi_mocks):
        """Test that config property returns the configuration."""
        config = EmailConfig(sender="test@example.com")
        email = Email("test", config=config)

        assert email.config == config
        assert email.config.sender == "test@example.com"

    @pulumi.runtime.test
    def test_email_name_property(self, pulumi_mocks):
        """Test that name property returns the component name."""
        email = Email("my-email", sender="test@example.com")

        assert email.name == "my-email"


class TestEmailDnsRecords:
    """Tests for Email component DNS record creation."""

    @pulumi.runtime.test
    def test_email_with_dns_false_creates_no_dns_records(self, pulumi_mocks):
        """Test that dns=False creates no DNS records but still creates verification."""
        email = Email("marketing", sender="example.com", dns=False)
        _ = email.resources

        def check_resources(_):
            # Verify domain identity was created
            identities = pulumi_mocks.created_ses_email_identities()
            assert len(identities) == 1
            assert identities[0].inputs["emailIdentity"] == "example.com"

            # Verify domain verification was created
            verifications = pulumi_mocks.created_ses_domain_verifications()
            assert len(verifications) == 1

        # Wait on domain_verification since it's created last for domain senders
        email.resources.domain_verification.arn.apply(check_resources)

    @pulumi.runtime.test
    def test_email_address_does_not_create_verification(self, pulumi_mocks):
        """Test that email address sender does not create domain verification."""
        email = Email("notifications", sender="test@example.com")
        _ = email.resources

        def check_no_verification(_):
            verifications = pulumi_mocks.created_ses_domain_verifications()
            assert len(verifications) == 0

        email.resources.identity.arn.apply(check_no_verification)


class TestEmailResourceNaming:
    """Tests for Email component resource naming."""

    @pulumi.runtime.test
    def test_configuration_set_naming(self, pulumi_mocks):
        """Test that configuration set has correct naming."""
        email = Email("alerts", sender="test@example.com")
        _ = email.resources

        def check_naming(_):
            config_sets = pulumi_mocks.created_ses_configuration_sets()
            assert len(config_sets) == 1
            # Name should include the prefix and component name
            assert "alerts-config" in config_sets[0].name

        email.resources.configuration_set.arn.apply(check_naming)

    @pulumi.runtime.test
    def test_identity_naming(self, pulumi_mocks):
        """Test that identity has correct naming."""
        email = Email("alerts", sender="test@example.com")
        _ = email.resources

        def check_naming(_):
            identities = pulumi_mocks.created_ses_email_identities()
            assert len(identities) == 1
            assert "alerts-identity" in identities[0].name

        email.resources.identity.arn.apply(check_naming)

    @pulumi.runtime.test
    def test_event_destination_naming(self, pulumi_mocks):
        """Test that event destinations have correct naming."""
        email = Email(
            "alerts",
            sender="test@example.com",
            events=[
                EventDestination(
                    name="errors",
                    types=["bounce"],
                    topic_arn="arn:aws:sns:us-east-1:123456789012:Topic"
                )
            ]
        )
        _ = email.resources

        def check_naming(_):
            event_destinations = pulumi_mocks.created_ses_event_destinations()
            assert len(event_destinations) == 1
            assert "alerts-event-errors" in event_destinations[0].name

        email.resources.identity.arn.apply(check_naming)
