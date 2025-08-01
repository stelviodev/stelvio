from dataclasses import dataclass
from typing import final
import pulumi
import pulumi_aws
from stelvio.component import Component
from stelvio.dns import Dns



@dataclass(frozen=True)
class AcmValidatedDomainResources:
    certificate: pulumi_aws.acm.Certificate
    validation_record: pulumi_aws.route53.Record
    cert_validation: pulumi_aws.acm.CertificateValidation


@final
class AcmValidatedDomain(Component[AcmValidatedDomainResources]):
    def __init__(self, name, domain_name: str, dns: Dns, prefix_fn: callable):
        self.dns = dns
        self.domain_name = domain_name
        self.prefix_fn = prefix_fn
        super().__init__(name)

    def _create_resources(self) -> AcmValidatedDomainResources:
    #     self.create()
    #     return AcmValidatedDomainResources(
    #         certificate=self.resources.certificate,
    #         validation_record=self.resources.validation_record,
    #         cert_validation=self.resources.cert_validation,
    #     )

    # def create(self):
        # 1 - Issue Certificate
        certificate = pulumi_aws.acm.Certificate(
            self.prefix_fn(f"{self.name}-certificate"),
            domain_name=self.domain_name,
            validation_method="DNS",
        )

        # 2 - Validate Certificate with DNS PROVIDER
        validation_record = self.dns.create_caa_record(
            resource_name=f"{self.prefix_fn(f'{self.name}certificate-validation-record')}",
            name=certificate.domain_validation_options[0].resource_record_name,
            type=certificate.domain_validation_options[0].resource_record_type,
            content=certificate.domain_validation_options[0].resource_record_value,
            ttl=1,
        )

        # 3 - Wait for validation - use the validation record's FQDN to ensure it exists
        cert_validation = pulumi_aws.acm.CertificateValidation(
            self.prefix_fn(f"{self.name}-certificate-validation"),
            certificate_arn=certificate.arn,
            validation_record_fqdns=[
                validation_record.name
            ],  # This ensures validation_record exists
            opts=pulumi.ResourceOptions(
                depends_on=[certificate, validation_record._pulumi_resource]
            ),
        )

        self._resources = AcmValidatedDomainResources(
            certificate=certificate,
            validation_record=validation_record._pulumi_resource,
            cert_validation=cert_validation,
        )
        return self._resources
