from dataclasses import dataclass
from typing import final

import pulumi_aws

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.dns import DnsProviderNotConfiguredError
from stelvio.link import Link, Linkable, LinkConfig


@final
@dataclass(frozen=True)
class EmailResources:
    sender: str
    identity: pulumi_aws.sesv2.EmailIdentity
    configuration_set: pulumi_aws.sesv2.ConfigurationSet | None = None


"""
TODO:
- Dmarc/spf/dkim setup
- Linkable implementation
- Verification status checking
- events
"""


@final
class Email(Component[EmailResources], Linkable):
    def __init__(self, name: str, sender: str, dmarc: str | None):
        super().__init__(name)
        self.sender = sender
        self.dmarc = dmarc

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
        is_domain = "@" not in self.sender
        if is_domain and context().dns is None:
            raise DnsProviderNotConfiguredError(
                "DNS provider must be configured to create domain email identity."
            )
        if is_domain:
            self.check_domain(self.sender)
        else:
            self.check_email(self.sender)

        # configuration_set = pulumi_aws.sesv2.ConfigurationSet(
        #     resource_name=f"{self.name}-config-set",
        #     name=f"{self.name}-config-set",
        # )

        identity = pulumi_aws.sesv2.EmailIdentity(
            resource_name=f"{self.name}-identity",
            email_identity=self.sender,
            # configuration_set_name=configuration_set.name,
        )

        return EmailResources(
            sender=self.sender,
            identity=identity,
            # configuration_set=configuration_set,
        )

    def link(self) -> Link:
        link_creator_ = ComponentRegistry.get_link_config_creator(type(self))

        link_config = link_creator_(self.resources.identity, self.resources.configuration_set)
        return Link(self.name, link_config.properties, link_config.permissions)


@link_config_creator(Email)
def default_bucket_link(
    identity: pulumi_aws.sesv2.EmailIdentity, configuration_set: pulumi_aws.sesv2.ConfigurationSet
) -> LinkConfig:
    return LinkConfig(
        properties={
            "email_identity_sender": identity.email_identity,
            "email_identity_arn": identity.arn,
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
