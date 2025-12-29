from dataclasses import dataclass
from typing import TypedDict, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.dns import Dns, DnsProviderNotConfiguredError
from stelvio.link import Link, Linkable, LinkConfig


@final
@dataclass(frozen=True)
class EmailResources:
    sender: str
    identity: pulumi_aws.sesv2.EmailIdentity
    configuration_set: pulumi_aws.sesv2.ConfigurationSet | None = None
    dkim_records: list[pulumi_aws.route53.Record] | None = None
    dmarc_record: pulumi_aws.route53.Record | None = None
    verification: pulumi_aws.ses.DomainIdentityVerification | None = None


class EventConfiguration(TypedDict):
    name: str
    types: list[str]
    topic_arn: str


@final
class Email(Component[EmailResources], Linkable):
    def __init__(
        self,
        name: str,
        sender: str,
        dmarc: str | None,
        events: list[EventConfiguration] | None = None,
        dns: Dns | None = None,
    ):
        super().__init__(name)
        self.sender = sender
        self.dmarc = dmarc
        self.events = events
        # We allow passing in a DNS provider since email verification may
        # need another DNS provider than the one in context
        if not dns:
            self.dns = context().dns
        else:
            self.dns = dns
        self.is_domain = "@" not in sender
        if self.dmarc and not self.is_domain:
            raise ValueError("DMARC can only be set for domain email identities.")
        if self.dmarc and not self.dns:
            raise DnsProviderNotConfiguredError(
                "DNS provider must be configured to set DMARC records."
            )
        if self.is_domain and not self.dns:
            raise DnsProviderNotConfiguredError(
                "DNS provider must be configured to create domain email identity."
            )
        if self.is_domain:
            self.check_domain(sender)
        else:
            self.check_email(sender)
        if self.dmarc is None:
            self.dmarc = "v=DMARC1; p=none;"
        self._resources = None

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
            resource_name=f"{self.name}-config-set",
            configuration_set_name=f"{self.name}-config-set",
        )

        identity = pulumi_aws.sesv2.EmailIdentity(
            resource_name=f"{self.name}-identity",
            email_identity=self.sender,
            configuration_set_name=configuration_set.configuration_set_name,
        )

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
                dkim_records.append(
                    self.dns.create_record(
                        resource_name=context().prefix(f"{self.name}-dkim-record-{i}"),
                        name=token.apply(lambda t: f"{t}._domainkey.{self.sender}"),
                        record_type="CNAME",
                        value=token.apply(lambda t: f"{t}.dkim.amazonses.com"),
                        ttl=600,
                    )
                )
            if self.dmarc:
                dmarc_record = self.dns.create_record(
                    resource_name=context().prefix(f"{self.name}-dmarc-record"),
                    name=f"_dmarc.{self.sender}"
                    if self.is_domain
                    else f"_dmarc.{self.sender.split('@')[1]}",
                    record_type="TXT",
                    value=self.dmarc,
                    ttl=600,
                )
            verification = pulumi_aws.ses.DomainIdentityVerification(
                resource_name=f"{self.name}-identity-verification",
                domain=identity.email_identity,
                opts=pulumi.ResourceOptions(depends_on=[identity]),
            )

        if self.events:
            for event in self.events:
                pulumi_aws.sesv2.EventDestination(
                    resource_name=context().prefix(f"{self.name}-event-{event['name']}"),
                    configuration_set_name=configuration_set.configuration_set_name,
                    event_destination_name=event["name"],
                    matching_event_types=event["types"],
                    sns_destination=pulumi_aws.sesv2.EventDestinationSnsDestinationArgs(
                        topic_arn=event["topic_arn"]
                    ),
                )

        return EmailResources(
            sender=self.sender,
            identity=identity,
            configuration_set=configuration_set,
            dkim_records=dkim_records,
            dmarc_record=dmarc_record,
            verification=verification,
        )

    def link(self) -> Link:
        link_creator_ = ComponentRegistry.get_link_config_creator(type(self))

        link_config = link_creator_(self.resources.identity, self.resources.configuration_set)
        return Link(self.name, link_config.properties, link_config.permissions)


@link_config_creator(Email)
def default_email_link(
    identity: pulumi_aws.sesv2.EmailIdentity, configuration_set: pulumi_aws.sesv2.ConfigurationSet
) -> LinkConfig:
    return LinkConfig(
        properties={
            "email_identity_sender": identity.email_identity,
            "email_identity_arn": identity.arn,
            "dkim_token_0": identity.dkim_signing_attributes.apply(
                lambda attrs: attrs["tokens"][0]
            ),
            "dkim_token_1": identity.dkim_signing_attributes.apply(
                lambda attrs: attrs["tokens"][1]
            ),
            "dkim_token_2": identity.dkim_signing_attributes.apply(
                lambda attrs: attrs["tokens"][2]
            ),
            "configuration_set_name": configuration_set.configuration_set_name,
            "configuration_set_arn": configuration_set.arn,
        },
        permissions=[
            AwsPermission(
                actions=["ses:*"],
                resources=[identity.arn, configuration_set.arn],
            ),
            AwsPermission(
                actions=["ses:SendEmail", "ses:SendRawEmail", "ses:SendTemplatedEmail"],
                resources=[identity.arn],
            ),
        ],
    )
