from dataclasses import dataclass
from typing import Any, Literal, TypedDict, Unpack, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator
from stelvio.dns import Dns, DnsProviderNotConfiguredError, Record
from stelvio.link import LinkableMixin, LinkConfig

__all__ = [
    "Email",
    "EmailConfig",
    "EmailConfigDict",
    "EmailResources",
    "EventConfiguration",
    "EventType",
]

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


@final
@dataclass(frozen=True, kw_only=True)
class EmailResources:
    identity: pulumi_aws.sesv2.EmailIdentity
    configuration_set: pulumi_aws.sesv2.ConfigurationSet | None = None
    dkim_records: list[Record] | None = None
    dmarc_record: Record | None = None
    verification: pulumi_aws.ses.DomainIdentityVerification | None = None
    event_destinations: list[pulumi_aws.sesv2.ConfigurationSetEventDestination] | None = None


class EmailCustomizationDict(TypedDict, total=False):
    identity: pulumi_aws.sesv2.EmailIdentityArgs | dict[str, Any] | None
    configuration_set: pulumi_aws.sesv2.ConfigurationSetArgs | dict[str, Any] | None
    dkim_records: dict[str, Any] | None  # no pulumi args here because cross cloud compat
    dmarc_record: dict[str, Any] | None  # no pulumi args here because cross cloud compat
    verification: pulumi_aws.ses.DomainIdentityVerificationArgs | dict[str, Any] | None
    event_destinations: (
        pulumi_aws.sesv2.ConfigurationSetEventDestinationArgs | dict[str, Any] | None
    )


class EventConfiguration(TypedDict, total=False):
    name: str
    types: list[EventType]
    topic_arn: str


class EmailConfigDict(TypedDict, total=False):
    """Dictionary configuration for the Email component."""

    sender: str
    dmarc: str | None | Literal[False]
    events: list[EventConfiguration] | None
    sandbox: bool
    dns: Dns | Literal[False] | None


@dataclass(frozen=True, kw_only=True)
class EmailConfig:
    """Typed configuration for the Email component."""

    sender: str
    dmarc: str | None = None
    events: list[EventConfiguration] | None = None
    sandbox: bool = False
    dns: Dns | Literal[False] | None = None


@final
class Email(Component[EmailResources, EmailCustomizationDict], LinkableMixin):
    _config: EmailConfig

    def __init__(
        self,
        name: str,
        config: EmailConfig | EmailConfigDict | None = None,
        customize: EmailCustomizationDict | None = None,
        **opts: Unpack[EmailConfigDict],
    ):
        super().__init__(name, customize=customize)
        self._config = self._parse_config(config, opts)
        self.is_domain = "@" not in self.config.sender
        # We allow passing in a DNS provider since email verification may
        # need another DNS provider than the one in context
        if not self.config.dns:
            self.dns = context().dns
        else:
            self.dns = self.config.dns

        if self.config.dmarc and not self.is_domain:
            raise ValueError("DMARC can only be set for domain email identities.")
        if self.config.dmarc and not self.dns:
            raise DnsProviderNotConfiguredError(
                "DNS provider must be configured to set DMARC records."
            )
        if self.is_domain and not self.dns:
            raise DnsProviderNotConfiguredError(
                "DNS provider must be configured to create domain email identity."
            )
        if self.is_domain:
            self.check_domain(self.config.sender)
        else:
            self.check_email(self.config.sender)

        self._resources = None

    @property
    def config(self) -> EmailConfig:
        """Get the component configuration."""
        return self._config

    @property
    def sender(self) -> str:
        return self.config.sender

    @property
    def dmarc(self) -> str | None:
        return self.config.dmarc

    @property
    def events(self) -> list[EventConfiguration] | None:
        return self.config.events

    @property
    def sandbox(self) -> bool:
        return self.config.sandbox

    @staticmethod
    def _parse_config(
        config: EmailConfig | EmailConfigDict | str | None, opts: EmailConfigDict
    ) -> EmailConfig:
        """Parse configuration from either typed or dict form."""
        if isinstance(config, dict | EmailConfig) and opts:
            raise ValueError(
                "Invalid configuration: cannot combine complete email "
                "configuration with additional options"
            )
        if isinstance(config, EmailConfig):
            pass
        elif isinstance(config, dict):
            config = EmailConfig(**config)
        elif isinstance(config, str):
            opts["sender"] = config
            config = EmailConfig(**opts)
        # First apply default DMARC for domains if dmarc is None (but not explicitly False)
        if config.dmarc is None and config.sender and "@" not in config.sender:
            config = EmailConfig(
                sender=config.sender,
                dmarc="v=DMARC1; p=none;",
                events=config.events,
                sandbox=config.sandbox,
                dns=config.dns,
            )
        # Then handle explicit dmarc=False to disable DMARC
        elif config.dmarc is False:
            config = EmailConfig(
                sender=config.sender,
                dmarc=None,
                events=config.events,
                sandbox=config.sandbox,
                dns=config.dns,
            )

        return config

    def check_domain(self, domain: str) -> None:
        """
        Checks if the domain is a valid domain.
        """
        if not isinstance(domain, str) or "." not in domain:
            raise ValueError(f"Invalid domain: {domain}")

    def check_email(self, email: str) -> None:
        """
        Checks if the email is a valid email.
        """
        if not isinstance(email, str) or "@" not in email:
            raise ValueError(f"Invalid email: {email}")

    def _create_resources(self) -> EmailResources:
        configuration_set = pulumi_aws.sesv2.ConfigurationSet(
            **self._customizer(
                "configuration_set",
                {
                    "resource_name": context().prefix(f"{self.name}-config-set"),
                    "configuration_set_name": f"{self.name}-config-set",
                },
            ),
        )

        identity = pulumi_aws.sesv2.EmailIdentity(
            **self._customizer(
                "identity",
                {
                    "resource_name": context().prefix(f"{self.name}-identity"),
                    "email_identity": self.sender,
                    "configuration_set_name": configuration_set.configuration_set_name,
                },
            ),
        )

        pulumi.export(f"{self.name}-ses-configuration-set-arn", configuration_set.arn)
        pulumi.export(f"{self.name}-ses-identity-arn", identity.arn)

        dkim_records = None
        dmarc_record = None
        verification = None
        if self.is_domain:
            dkim_records = []
            # SES always returns 3 tokens
            for i in range(3):
                token = identity.dkim_signing_attributes.apply(
                    lambda attrs, i=i: attrs["tokens"][i]
                )
                record = self.dns.create_record(
                    resource_name=context().prefix(f"{self.name}-dkim-record-{i}"),
                    **self._customizer(
                        "dkim_records",
                        {
                            "name": token.apply(lambda t: f"{t}._domainkey.{self.sender}"),
                            "value": token.apply(lambda t: f"{t}.dkim.amazonses.com"),
                            "record_type": "CNAME",
                            "ttl": 600,
                        },
                    ),
                )
                dkim_records.append(record)
                pulumi.export(f"{self.name}-dkim-record-{i}-name", record.name)
                pulumi.export(f"{self.name}-dkim-record-{i}-value", record.value)

            if self.dmarc:
                dmarc_record = self.dns.create_record(
                    resource_name=context().prefix(f"{self.name}-dmarc-record"),
                    name=f"_dmarc.{self.sender}",
                    **self._customizer(
                        "dmarc_record",
                        {
                            "record_type": "TXT",
                            "value": self.dmarc,
                            "ttl": 600,
                        },
                    ),
                )
                pulumi.export(f"{self.name}-dmarc-record-name", dmarc_record.name)
                pulumi.export(f"{self.name}-dmarc-record-value", dmarc_record.value)
            verification = pulumi_aws.ses.DomainIdentityVerification(
                resource_name=context().prefix(f"{self.name}-identity-verification"),
                **self._customizer(
                    "verification",
                    {
                        "domain": identity.email_identity,
                    },
                ),
                opts=pulumi.ResourceOptions(depends_on=[identity]),
            )
            pulumi.export(f"{self.name}-ses-domain-verification-token-arn", verification.arn)
        event_destinations = []
        if self.events:
            for event in self.events:
                event_destination = pulumi_aws.sesv2.ConfigurationSetEventDestination(
                    resource_name=context().prefix(f"{self.name}-event-{event['name']}"),
                    **self._customizer(
                        "event_destinations",
                        {
                            "configuration_set_name": configuration_set.configuration_set_name,
                            "event_destination_name": event["name"],
                            "event_destination": pulumi_aws.sesv2.ConfigurationSetEventDestinationEventDestinationArgs(  # noqa: E501
                                enabled=True,
                                matching_event_types=event["types"],
                                sns_destination=pulumi_aws.sesv2.ConfigurationSetEventDestinationEventDestinationSnsDestinationArgs(
                                    topic_arn=event["topic_arn"]
                                ),
                            ),
                        },
                    ),
                )
                event_destinations.append(event_destination)
                pulumi.export(f"{self.name}-ses-event-{event['name']}-id", event_destination.id)

        return EmailResources(
            identity=identity,
            configuration_set=configuration_set,
            dkim_records=dkim_records,
            dmarc_record=dmarc_record,
            verification=verification,
            event_destinations=event_destinations,
        )


@link_config_creator(Email)
def default_email_link(
    email: Email,
) -> LinkConfig:
    identity = email.resources.identity
    configuration_set = email.resources.configuration_set
    sandbox = email.sandbox
    return LinkConfig(
        properties={
            "email_identity_sender": identity.email_identity,
            "email_identity_arn": identity.arn,
            "configuration_set_name": configuration_set.configuration_set_name,
            "configuration_set_arn": configuration_set.arn,
        },
        permissions=[
            AwsPermission(
                actions=["ses:SendEmail", "ses:SendRawEmail", "ses:SendTemplatedEmail"],
                resources=[identity.arn if not sandbox else "*"],
            ),
        ],
    )
