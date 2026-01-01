"""AWS SES Email component for Stelvio.

This module provides the Email component for sending emails via Amazon Simple Email Service (SES).
It supports both email address and domain-based senders, with automatic DNS verification for domains.
"""

from dataclasses import dataclass, field
from typing import Literal, TypedDict, Unpack, final

import pulumi
from pulumi import Input, Output, ResourceOptions
from pulumi_aws import ses, sesv2

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.dns import Dns
from stelvio.link import Link, Linkable, LinkConfig


__all__ = [
    "Email",
    "EmailConfig",
    "EmailConfigDict",
    "EmailResources",
    "EventType",
    "EventDestination",
    "EventDestinationDict",
]


# Event types supported by SES
EventType = Literal[
    "send",
    "reject",
    "bounce",
    "complaint",
    "delivery",
    "delivery-delay",
    "rendering-failure",
    "subscription",
    "open",
    "click",
]


class EventDestinationDict(TypedDict, total=False):
    """Dictionary configuration for an event destination."""

    name: str
    types: list[EventType]
    topic_arn: str | None
    bus_arn: str | None


@dataclass(frozen=True)
class EventDestination:
    """Configuration for an SES event destination.

    Args:
        name: The name of the event destination.
        types: List of event types to capture.
        topic_arn: ARN of an SNS topic to send events to.
        bus_arn: ARN of an EventBridge bus to send events to.

    Raises:
        ValueError: If neither topic_arn nor bus_arn is provided.
    """

    name: str
    types: list[EventType]
    topic_arn: str | None = None
    bus_arn: str | None = None

    def __post_init__(self) -> None:
        if not self.topic_arn and not self.bus_arn:
            raise ValueError(
                f"Event destination '{self.name}' requires either 'topic_arn' or 'bus_arn'"
            )
        if self.topic_arn and self.bus_arn:
            raise ValueError(
                f"Event destination '{self.name}' cannot have both 'topic_arn' and 'bus_arn'"
            )


class EmailConfigDict(TypedDict, total=False):
    """Dictionary configuration for the Email component."""

    sender: str
    dns: Dns | Literal[False] | None
    dmarc: str | None
    events: list[EventDestination | EventDestinationDict]


@dataclass(frozen=True, kw_only=True)
class EmailConfig:
    """Configuration for the Email component.

    Args:
        sender: The email address or domain to send emails from.
        dns: DNS provider for automatic domain verification. Set to False to manage DNS manually.
             Only applicable when sender is a domain.
        dmarc: DMARC policy for the domain. Only applicable when sender is a domain.
        events: Event destinations for tracking email events.
    """

    sender: str
    dns: Dns | Literal[False] | None = None
    dmarc: str | None = None
    events: list[EventDestination] = field(default_factory=list)

    @property
    def is_domain(self) -> bool:
        """Check if the sender is a domain (not an email address)."""
        return "@" not in self.sender

    @property
    def normalized_dmarc(self) -> str:
        """Get the DMARC policy, defaulting to 'v=DMARC1; p=none;' if not set."""
        return self.dmarc or "v=DMARC1; p=none;"

    def __post_init__(self) -> None:
        if not self.sender:
            raise ValueError("sender is required")

        # Validate dns and dmarc are only used with domain senders
        if not self.is_domain:
            if self.dns is not None and self.dns is not False:
                raise ValueError("'dns' is only valid when 'sender' is a domain, not an email address")
            if self.dmarc is not None:
                raise ValueError("'dmarc' is only valid when 'sender' is a domain, not an email address")


@final
@dataclass(frozen=True)
class EmailResources:
    """Resources created by the Email component.

    Attributes:
        identity: The SES email identity resource.
        configuration_set: The SES configuration set resource.
        domain_verification: The domain verification resource (only for domain senders).
        event_destinations: List of event destination resources.
    """

    identity: sesv2.EmailIdentity
    configuration_set: sesv2.ConfigurationSet
    domain_verification: ses.DomainIdentityVerification | None = None
    event_destinations: list[sesv2.ConfigurationSetEventDestination] = field(default_factory=list)


@final
class Email(Component[EmailResources], Linkable):
    """AWS SES Email component for sending emails.

    This component creates an SES email identity for sending emails. It supports both
    email address and domain-based senders.

    For email addresses:
        - You'll receive a verification email from AWS when you deploy.
        - Click the link to verify your email address.

    For domains:
        - DNS records for DKIM and DMARC will be created automatically if a DNS provider is configured.
        - The component will wait for domain verification before completing.

    Note:
        New AWS SES accounts are in "sandbox mode" and can only send to verified addresses.
        Request production access from AWS to remove this restriction.

    Args:
        name: Component name (used for resource naming).
        config: Complete configuration as EmailConfig or dict.
        **opts: Individual configuration parameters.

    Examples:
        Using an email address (requires email verification):

            email = Email("notifications", sender="alerts@gmail.com")

        Using a domain with automatic DNS verification:

            from stelvio.aws.dns import Route53Dns

            email = Email(
                "marketing",
                sender="example.com",
                dns=Route53Dns(zone_id="Z1234567890")
            )

        Linking to a function:

            from stelvio.aws.function import Function

            email = Email("alerts", sender="alerts@example.com")
            handler = Function("send-alert", handler="functions/alert.handler", links=[email.link()])
    """

    _config: EmailConfig

    def __init__(
        self,
        name: str,
        config: EmailConfig | EmailConfigDict | None = None,
        **opts: Unpack[EmailConfigDict],
    ):
        super().__init__(name)
        self._config = self._parse_config(config, opts)

    @staticmethod
    def _parse_config(
        config: EmailConfig | EmailConfigDict | None, opts: EmailConfigDict
    ) -> EmailConfig:
        """Parse and validate configuration from various input formats."""
        if config is not None and opts:
            raise ValueError("Cannot provide both 'config' and individual parameters")

        if isinstance(config, EmailConfig):
            return config

        # Merge config dict with opts (opts takes precedence)
        merged = {**(config or {}), **opts}

        if not merged:
            raise ValueError("Email requires at least a 'sender' parameter")

        # Normalize events to EventDestination dataclass instances
        events = merged.get("events", [])
        normalized_events = []
        for event in events:
            if isinstance(event, EventDestination):
                normalized_events.append(event)
            else:
                normalized_events.append(EventDestination(**event))
        merged["events"] = normalized_events

        return EmailConfig(**merged)

    @property
    def config(self) -> EmailConfig:
        """Get the component configuration."""
        return self._config

    @property
    def sender(self) -> Output[str]:
        """The sender email address or domain."""
        return self.resources.identity.email_identity

    @property
    def config_set_name(self) -> Output[str]:
        """The name of the SES configuration set."""
        return self.resources.configuration_set.configuration_set_name

    @property
    def identity_arn(self) -> Output[str]:
        """The ARN of the SES email identity."""
        return self.resources.identity.arn

    def _create_resources(self) -> EmailResources:
        """Create the AWS SES resources for the email component."""
        # Create configuration set first (identity references it)
        configuration_set = self._create_configuration_set()

        # Create the email identity
        identity = self._create_identity(configuration_set)

        # Create event destinations
        event_destinations = self._create_event_destinations(configuration_set)

        # Handle domain-specific setup
        domain_verification = None
        if self._config.is_domain:
            self._create_dns_records(identity)
            domain_verification = self._create_domain_verification(identity)

        # Export outputs
        pulumi.export(f"email_{self.name}_sender", identity.email_identity)
        pulumi.export(f"email_{self.name}_identity_arn", identity.arn)
        pulumi.export(f"email_{self.name}_config_set", configuration_set.configuration_set_name)

        return EmailResources(
            identity=identity,
            configuration_set=configuration_set,
            domain_verification=domain_verification,
            event_destinations=event_destinations,
        )

    def _create_configuration_set(self) -> sesv2.ConfigurationSet:
        """Create the SES configuration set."""
        return sesv2.ConfigurationSet(
            context().prefix(f"{self.name}-config"),
            configuration_set_name=context().prefix(f"{self.name}-config"),
        )

    def _create_identity(self, configuration_set: sesv2.ConfigurationSet) -> sesv2.EmailIdentity:
        """Create the SES email identity."""
        return sesv2.EmailIdentity(
            context().prefix(f"{self.name}-identity"),
            email_identity=self._config.sender,
            configuration_set_name=configuration_set.configuration_set_name,
        )

    def _create_event_destinations(
        self, configuration_set: sesv2.ConfigurationSet
    ) -> list[sesv2.ConfigurationSetEventDestination]:
        """Create event destinations for tracking email events."""
        destinations = []

        for event in self._config.events:
            # Convert event types to SES format (uppercase with underscores)
            matching_event_types = [
                event_type.upper().replace("-", "_") for event_type in event.types
            ]

            # Build event destination config
            event_destination_config: dict = {
                "matching_event_types": matching_event_types,
                "enabled": True,
            }

            if event.topic_arn:
                event_destination_config["sns_destination"] = {
                    "topic_arn": event.topic_arn,
                }
            elif event.bus_arn:
                event_destination_config["event_bridge_destination"] = {
                    "event_bus_arn": event.bus_arn,
                }

            destination = sesv2.ConfigurationSetEventDestination(
                context().prefix(f"{self.name}-event-{event.name}"),
                configuration_set_name=configuration_set.configuration_set_name,
                event_destination_name=event.name,
                event_destination=event_destination_config,
            )
            destinations.append(destination)

        return destinations

    def _create_dns_records(self, identity: sesv2.EmailIdentity) -> None:
        """Create DNS records for domain verification (DKIM and DMARC)."""
        dns_provider = self._get_dns_provider()
        if dns_provider is None:
            return

        # Create DKIM records
        identity.dkim_signing_attributes.tokens.apply(
            lambda tokens: self._create_dkim_records(dns_provider, tokens) if tokens else None
        )

        # Create DMARC record
        dns_provider.create_record(
            context().prefix(f"{self.name}-dmarc"),
            name=f"_dmarc.{self._config.sender}",
            record_type="TXT",
            value=self._config.normalized_dmarc,
            ttl=300,
        )

    def _create_dkim_records(self, dns_provider: Dns, tokens: list[str]) -> None:
        """Create DKIM CNAME records for email authentication."""
        for i, token in enumerate(tokens):
            dns_provider.create_record(
                context().prefix(f"{self.name}-dkim-{i}"),
                name=f"{token}._domainkey.{self._config.sender}",
                record_type="CNAME",
                value=f"{token}.dkim.amazonses.com",
                ttl=300,
            )

    def _create_domain_verification(
        self, identity: sesv2.EmailIdentity
    ) -> ses.DomainIdentityVerification:
        """Create the domain verification waiter resource."""
        return ses.DomainIdentityVerification(
            context().prefix(f"{self.name}-verification"),
            domain=self._config.sender,
            opts=ResourceOptions(depends_on=[identity]),
        )

    def _get_dns_provider(self) -> Dns | None:
        """Get the DNS provider for domain verification."""
        # If dns is explicitly False, no DNS provider
        if self._config.dns is False:
            return None

        # If dns is provided, use it
        if self._config.dns is not None:
            return self._config.dns

        # Try to get DNS provider from context
        dns_from_context = context().dns
        return dns_from_context

    def link(self) -> Link:
        """Create a link for connecting this email component to functions.

        Returns:
            Link: A link object with email properties and SES permissions.
        """
        link_creator_ = ComponentRegistry.get_link_config_creator(type(self))
        if link_creator_ is None:
            raise RuntimeError(f"No link creator registered for {type(self)}")

        link_config = link_creator_(self.resources)
        return Link(self.name, link_config.properties, link_config.permissions)


@link_config_creator(Email)
def default_email_link(resources: EmailResources) -> LinkConfig:
    """Create default link configuration for the Email component.

    Provides:
        - Properties: sender, config_set
        - Permissions: Full SES access to identity and config set, plus send permissions
    """
    return LinkConfig(
        properties={
            "sender": resources.identity.email_identity,
            "config_set": resources.configuration_set.configuration_set_name,
        },
        permissions=[
            # Full access to the identity and configuration set
            AwsPermission(
                actions=["ses:*"],
                resources=[
                    resources.identity.arn,
                    resources.configuration_set.arn,
                ],
            ),
            # Send permissions (need wildcard for sandbox mode recipients)
            AwsPermission(
                actions=[
                    "ses:SendEmail",
                    "ses:SendRawEmail",
                    "ses:SendTemplatedEmail",
                ],
                resources=["*"],
            ),
        ],
    )
