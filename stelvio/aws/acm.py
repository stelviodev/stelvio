from dataclasses import dataclass
from typing import final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.component import Component
from stelvio.dns import DnsProviderNotConfiguredError, Record


@final
@dataclass(frozen=True)
class AcmValidatedDomainResources:
    certificate: pulumi_aws.acm.Certificate
    validation_record: Record
    cert_validation: pulumi_aws.acm.CertificateValidation


@final
class AcmValidatedDomain(Component[AcmValidatedDomainResources]):
    def __init__(self, name: str, domain_name: str):
        self.domain_name = domain_name
        super().__init__(name)

    def _create_resources(self) -> AcmValidatedDomainResources:
        dns = context().dns
        if dns is None:
            raise DnsProviderNotConfiguredError(
                "DNS provider is not configured in the context. "
                "Please set up a DNS provider to use custom domains."
            )

        # 1 - Issue Certificate
        certificate = pulumi_aws.acm.Certificate(
            context().prefix(f"{self.name}-certificate"),
            domain_name=self.domain_name,
            validation_method="DNS",
        )

        # 2 - Validate Certificate with DNS PROVIDER
        first_option = certificate.domain_validation_options.apply(lambda options: options[0])
        validation_record = dns.create_caa_record(
            resource_name=context().prefix(f"{self.name}-certificate-validation-record"),
            name=first_option.apply(lambda opt: opt["resource_record_name"]),
            record_type=first_option.apply(lambda opt: opt["resource_record_type"]),
            content=first_option.apply(lambda opt: opt["resource_record_value"]),
            ttl=1,
        )

        # 3 - Wait for validation - use the validation record's FQDN to ensure it exists
        cert_validation = pulumi_aws.acm.CertificateValidation(
            context().prefix(f"{self.name}-certificate-validation"),
            certificate_arn=certificate.arn,
            # This ensures validation_record exists
            validation_record_fqdns=[validation_record.name],
            opts=pulumi.ResourceOptions(
                depends_on=[certificate, validation_record.pulumi_resource]
            ),
        )

        return AcmValidatedDomainResources(
            certificate=certificate,
            validation_record=validation_record,
            cert_validation=cert_validation,
        )
