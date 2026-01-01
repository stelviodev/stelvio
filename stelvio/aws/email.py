"""Email component for sending emails using AWS SES."""

from dataclasses import dataclass, field
from typing import Literal, TypedDict, Unpack, final

import pulumi
from pulumi import Output
from pulumi_aws import ses, sesv2

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig

EventTypeLiteral = Literal[
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


class EventDict(TypedDict, total=False):
    """Event notification configuration."""

    name: str
    types: list[EventTypeLiteral]
    topic: str | None
    bus: str | None


@dataclass(frozen=True)
class Event:
    """Event notification configuration for the Email component.

    Args:
        name: The name of the event destination.
        types: The types of events to send notifications for.
        topic: The ARN of the SNS topic to send events to.
        bus: The ARN of the EventBridge bus to send events to.
    """

    name: str
    types: list[EventTypeLiteral]
    topic: str | None = None
    bus: str | None = None

    def __post_init__(self) -> None:
        if not self.topic and not self.bus:
            raise ValueError("Event must have either 'topic' or 'bus' specified")
        if self.topic and self.bus:
            raise ValueError("Event cannot have both 'topic' and 'bus' specified")
        if not self.types:
            raise ValueError("Event must have at least one event type")


class EmailConfigDict(TypedDict, total=False):
    """Configuration options for Email component."""

    sender: str
    dmarc: str
    events: list[Event | EventDict]


@dataclass(frozen=True, kw_only=True)
class EmailConfig:
    """Configuration for the Email component.

    Args:
        sender: The email address or domain name to send emails from.
        dmarc: The DMARC policy for the domain (only for domain senders).
        events: Event notification configurations.
    """

    sender: str
    dmarc: str | None = None
    events: list[Event | EventDict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.sender:
            raise ValueError("sender is required")
        if self.dmarc and self.is_email:
            raise ValueError("DMARC can only be set for domain senders, not email addresses")

    @property
    def is_email(self) -> bool:
        """Check if sender is an email address (contains @)."""
        return "@" in self.sender

    @property
    def is_domain(self) -> bool:
        """Check if sender is a domain (no @)."""
        return not self.is_email


@final
@dataclass(frozen=True)
class EmailResources:
    """Resources created by Email component."""

    identity: sesv2.EmailIdentity
    configuration_set: sesv2.ConfigurationSet
    domain_verification: ses.DomainIdentityVerification | None = None


@final
class Email(Component[EmailResources], Linkable):
    """Send emails using Amazon Simple Email Service (SES).

    The Email component creates an SES email identity and configuration set for
    sending emails. You can send from either a verified email address or a domain.

    :::tip
    New AWS SES accounts are in sandbox mode and can only send to verified email
    addresses. Request production access to remove these restrictions.
    :::

    Args:
        name: Unique name for the component.
        sender: The email address or domain name to send emails from.
            Use email address format (e.g., "user@example.com") or domain
            (e.g., "example.com"). Email addresses require verification via link.
            Domains require DNS verification (DKIM records).
        dmarc: The DMARC policy for the domain (default: "v=DMARC1; p=none;").
            Only valid when sender is a domain.
        events: Event notification configurations for delivery tracking.
        **opts: Additional configuration options.

    Examples:
        Basic email sender:
            ```python
            from stelvio.aws.email import Email

            email = Email("my-email", sender="user@example.com")
            ```

        Domain sender:
            ```python
            Email("my-email", sender="example.com")
            ```

        With DMARC policy:
            ```python
            Email("my-email",
                sender="example.com",
                dmarc="v=DMARC1; p=quarantine; adkim=s; aspf=s;"
            )
            ```

        With event notifications:
            ```python
            from stelvio.aws.email import Email, Event

            email = Email("my-email",
                sender="example.com",
                events=[
                    Event(
                        name="bounces",
                        types=["bounce", "complaint"],
                        topic="arn:aws:sns:us-east-1:123456789:my-topic"
                    )
                ]
            )
            ```

        Link to a Lambda function:
            ```python
            from stelvio.aws.email import Email
            from stelvio.aws.function import Function

            email = Email("my-email", sender="user@example.com")

            Function("email-sender",
                handler="functions/sender.handler",
                links=[email]
            )
            ```
    """

    def __init__(
        self,
        name: str,
        sender: str | None = None,
        /,
        *,
        config: EmailConfig | EmailConfigDict | None = None,
        dmarc: str | None = None,
        events: list[Event | EventDict] | None = None,
        **opts: Unpack[EmailConfigDict],
    ):
        super().__init__(name)
        self._config = self._parse_config(sender, config, dmarc, events, opts)

    @staticmethod
    def _parse_config(
        sender: str | None,
        config: EmailConfig | EmailConfigDict | None,
        dmarc: str | None,
        events: list[Event | EventDict] | None,
        opts: EmailConfigDict,
    ) -> EmailConfig:
        """Parse and validate configuration."""
        # Check for conflicting config styles
        has_direct_args = sender is not None or dmarc is not None or events is not None
        has_opts = bool(opts)

        if config is not None and (has_direct_args or has_opts):
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with additional options "
                "- provide all settings either in 'config' or as separate options"
            )

        if config is not None:
            if isinstance(config, EmailConfig):
                return config
            return EmailConfig(**config)

        # Build config from direct args and opts
        final_sender = sender or opts.get("sender")
        if not final_sender:
            raise ValueError("sender is required")

        final_dmarc = dmarc or opts.get("dmarc")
        final_events = events or opts.get("events", [])

        return EmailConfig(sender=final_sender, dmarc=final_dmarc, events=list(final_events))

    @property
    def sender(self) -> str:
        """The sender email address or domain name."""
        return self._config.sender

    @property
    def config_set_name(self) -> Output[str]:
        """The name of the SES configuration set."""
        return self.resources.configuration_set.configuration_set_name

    @property
    def identity_arn(self) -> Output[str]:
        """The ARN of the SES email identity."""
        return self.resources.identity.arn

    def _create_resources(self) -> EmailResources:
        """Create the SES resources."""
        configuration_set = self._create_configuration_set()
        identity = self._create_identity(configuration_set)
        self._create_events(configuration_set)

        domain_verification = None
        if self._config.is_domain:
            domain_verification = self._create_domain_verification(identity)

        # Export key values
        pulumi.export(f"email_{self.name}_sender", self._config.sender)
        pulumi.export(f"email_{self.name}_config_set", configuration_set.configuration_set_name)

        return EmailResources(
            identity=identity,
            configuration_set=configuration_set,
            domain_verification=domain_verification,
        )

    def _create_configuration_set(self) -> sesv2.ConfigurationSet:
        """Create the SES configuration set."""
        # Use the prefixed name as the configuration set name
        config_set_name = context().prefix(f"{self.name}-config")
        return sesv2.ConfigurationSet(
            config_set_name,
            configuration_set_name=config_set_name,
        )

    def _create_identity(self, configuration_set: sesv2.ConfigurationSet) -> sesv2.EmailIdentity:
        """Create the SES email identity."""
        return sesv2.EmailIdentity(
            context().prefix(f"{self.name}-identity"),
            email_identity=self._config.sender,
            configuration_set_name=configuration_set.configuration_set_name,
        )

    def _create_events(self, configuration_set: sesv2.ConfigurationSet) -> None:
        """Create event destinations for the configuration set."""
        for event_config in self._config.events:
            event = event_config if isinstance(event_config, Event) else Event(**event_config)

            # Convert event types to SES format (uppercase with underscores)
            matching_event_types = [t.upper().replace("-", "_") for t in event.types]

            destination_args: dict = {
                "matching_event_types": matching_event_types,
                "enabled": True,
            }

            if event.topic:
                destination_args["sns_destination"] = {"topic_arn": event.topic}
            elif event.bus:
                destination_args["event_bridge_destination"] = {"event_bus_arn": event.bus}

            sesv2.ConfigurationSetEventDestination(
                context().prefix(f"{self.name}-event-{event.name}"),
                configuration_set_name=configuration_set.configuration_set_name,
                event_destination_name=event.name,
                event_destination=destination_args,
            )

    def _create_domain_verification(
        self, identity: sesv2.EmailIdentity
    ) -> ses.DomainIdentityVerification:
        """Create domain verification resource (only for domain senders)."""
        return ses.DomainIdentityVerification(
            context().prefix(f"{self.name}-verification"),
            domain=self._config.sender,
            opts=pulumi.ResourceOptions(depends_on=[identity]),
        )

    def link(self) -> Link:
        """Create a link for use with Lambda functions."""
        link_creator = ComponentRegistry.get_link_config_creator(type(self))
        if link_creator:
            link_config = link_creator(self.resources)
            return Link(
                name=self.name,
                properties=link_config.properties,
                permissions=link_config.permissions,
                component=self,
            )
        raise ValueError(f"No link creator registered for {type(self)}")


@link_config_creator(Email)
def _create_email_link(resources: EmailResources) -> LinkConfig:
    """Create link configuration for Email component.

    Provides properties and permissions needed to send emails from Lambda functions.
    """
    return LinkConfig(
        properties={
            "sender": resources.identity.email_identity,
            "config_set": resources.configuration_set.configuration_set_name,
        },
        permissions=[
            # General SES permissions on identity and configuration set
            AwsPermission(
                actions=["ses:*"],
                resources=[resources.identity.arn, resources.configuration_set.arn],
            ),
            # Send email permissions - need wildcard for sandbox mode recipients
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
