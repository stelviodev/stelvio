from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, TypedDict, Unpack, cast, final

import pulumi_aws
from pulumi import Output

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator
from stelvio.dns import Dns, DnsProviderNotConfiguredError, Record
from stelvio.link import LinkableMixin, LinkConfig

if TYPE_CHECKING:
    from pulumi_aws.ses import DomainIdentityVerificationArgs
    from pulumi_aws.sesv2 import (
        ConfigurationSetArgs,
        ConfigurationSetEventDestinationArgs,
        EmailIdentityArgs,
    )

    from stelvio.customize import Customization, CustomizationNoArgs

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
    configuration_set: pulumi_aws.sesv2.ConfigurationSet
    dkim_records: list[Record] | None = None
    dmarc_record: Record | None = None
    verification: pulumi_aws.ses.DomainIdentityVerification | None = None
    event_destinations: list[pulumi_aws.sesv2.ConfigurationSetEventDestination] | None = None


class EmailCustomizationDict(TypedDict, total=False):
    identity: Customization[EmailIdentityArgs]
    configuration_set: Customization[ConfigurationSetArgs]
    dkim_records: CustomizationNoArgs  # no pulumi args here because cross cloud compat
    dmarc_record: CustomizationNoArgs  # no pulumi args here because cross cloud compat
    verification: Customization[DomainIdentityVerificationArgs]
    event_destinations: Customization[ConfigurationSetEventDestinationArgs]


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
        *,
        tags: dict[str, str] | None = None,
        customize: EmailCustomizationDict | None = None,
        **opts: Unpack[EmailConfigDict],
    ):
        super().__init__("stelvio:aws:Email", name, tags=tags, customize=customize)
        self._config = self._parse_config(config, opts)
        self.is_domain = "@" not in self.config.sender
        # We allow passing in a DNS provider since email verification may
        # need another DNS provider than the one in context. Pass dns=False
        # to opt out of Stelvio managing DNS records for this email entirely
        # (DKIM, DMARC and domain verification are skipped — you handle them).
        dns_opted_out = self.config.dns is False

        if dns_opted_out:
            self.dns = None
        elif self.config.dns is None:
            self.dns = context().dns
        else:
            self.dns = self.config.dns

        if self.config.dmarc and not self.is_domain:
            raise ValueError("DMARC can only be set for domain email identities.")
        if self.config.dmarc and not self.dns and not dns_opted_out:
            raise DnsProviderNotConfiguredError(
                "DNS provider must be configured to set DMARC records."
            )
        if self.is_domain and not self.dns and not dns_opted_out:
            raise DnsProviderNotConfiguredError(
                "DNS provider must be configured to create domain email identity."
            )
        if self.is_domain:
            self.check_domain(self.config.sender)
        else:
            self.check_email(self.config.sender)

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
        dmarc_explicitly_disabled = False
        if config is None and not opts:
            raise ValueError(
                "Missing email sender: must provide either a complete configuration via "
                "'config' parameter or at least the 'sender' option"
            )
        if config is not None and not isinstance(config, str) and opts:
            raise ValueError(
                "Invalid configuration: cannot combine complete email "
                "configuration with additional options"
            )
        if isinstance(config, EmailConfig):
            parsed_config = config
        elif isinstance(config, dict):
            dmarc_explicitly_disabled = config.get("dmarc") is False
            parsed_config = _email_config_from_dict(config)
        elif isinstance(config, str):
            dmarc_explicitly_disabled = opts.get("dmarc") is False
            opts["sender"] = config
            parsed_config = _email_config_from_dict(opts)
        elif config is None:
            dmarc_explicitly_disabled = opts.get("dmarc") is False
            parsed_config = _email_config_from_dict(opts)
        else:
            raise TypeError(
                "Invalid config type: expected EmailConfig, dict, or str; "
                f"got {type(config).__name__}"
            )
        # First apply default DMARC for domains if dmarc is None (but not explicitly False).
        # Skip default if user opted out of DNS — we can't create the DMARC record anyway.
        if (
            parsed_config.dmarc is None
            and parsed_config.sender
            and "@" not in parsed_config.sender
            and parsed_config.dns is not False
            and not dmarc_explicitly_disabled
        ):
            parsed_config = EmailConfig(
                sender=parsed_config.sender,
                dmarc="v=DMARC1; p=none;",
                events=parsed_config.events,
                sandbox=parsed_config.sandbox,
                dns=parsed_config.dns,
            )

        return parsed_config

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
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        identity = pulumi_aws.sesv2.EmailIdentity(
            **self._customizer(
                "identity",
                {
                    "resource_name": context().prefix(f"{self.name}-identity"),
                    "email_identity": self.sender,
                    "configuration_set_name": configuration_set.configuration_set_name,
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
        )

        dkim_records = None
        dmarc_record = None
        verification = None
        if self.is_domain and self.dns:
            dkim_records = []
            dkim_signing_attributes = cast(
                "Output[dict[str, Any]]", identity.dkim_signing_attributes
            )
            # SES always returns 3 tokens
            for i in range(3):
                token = Output.apply(
                    dkim_signing_attributes,
                    lambda attrs, i=i: cast("str", attrs["tokens"][i]),
                )
                record = self.dns.create_record(
                    resource_name=context().prefix(f"{self.name}-dkim-record-{i}"),
                    **self._customizer(
                        "dkim_records",
                        {
                            "name": Output.apply(token, lambda t: f"{t}._domainkey.{self.sender}"),
                            "value": Output.apply(token, lambda t: f"{t}.dkim.amazonses.com"),
                            "record_type": "CNAME",
                        },
                        default_props={
                            "ttl": 600,
                        },
                    ),
                )
                dkim_records.append(record)

            if self.dmarc:
                dmarc_record = self.dns.create_record(
                    resource_name=context().prefix(f"{self.name}-dmarc-record"),
                    name=f"_dmarc.{self.sender}",
                    **self._customizer(
                        "dmarc_record",
                        {
                            "record_type": "TXT",
                            "value": self.dmarc,
                        },
                        default_props={
                            "ttl": 600,
                        },
                    ),
                )
            verification = pulumi_aws.ses.DomainIdentityVerification(
                resource_name=context().prefix(f"{self.name}-identity-verification"),
                **self._customizer(
                    "verification",
                    {
                        "domain": identity.email_identity,
                    },
                ),
                opts=self._resource_opts(depends_on=[identity]),
            )
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
                    opts=self._resource_opts(),
                )
                event_destinations.append(event_destination)

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


def _email_config_from_dict(config: EmailConfigDict) -> EmailConfig:
    dmarc = config.get("dmarc")
    return EmailConfig(
        sender=config["sender"],
        dmarc=None if dmarc is False else dmarc,
        events=config.get("events"),
        sandbox=config.get("sandbox", False),
        dns=config.get("dns"),
    )
