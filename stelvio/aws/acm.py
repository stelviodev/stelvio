from dataclasses import dataclass
from typing import Any, TypedDict, final

import pulumi_aws

from stelvio import context
from stelvio.component import Component
from stelvio.dns import DnsProviderNotConfiguredError, Record
from stelvio.provider import ProviderStore


@final
@dataclass(frozen=True)
class AcmValidatedDomainResources:
    certificate: pulumi_aws.acm.Certificate
    validation_record: Record
    cert_validation: pulumi_aws.acm.CertificateValidation


class AcmValidatedDomainCustomizationDict(TypedDict, total=False):
    certificate: pulumi_aws.acm.CertificateArgs | dict[str, Any] | None
    validation_record: (
        dict[str, Any] | None
    )  # No specific Plumi Args type here, because cross cloud compat
    cert_validation: pulumi_aws.acm.CertificateValidationArgs | dict[str, Any] | None


@final
class AcmValidatedDomain(
    Component[AcmValidatedDomainResources, AcmValidatedDomainCustomizationDict]
):
    def __init__(
        self,
        name: str,
        domain_name: str,
        region: str | None = None,
        *,
        tags: dict[str, str] | None = None,
        customize: AcmValidatedDomainCustomizationDict | None = None,
    ):
        super().__init__("stelvio:aws:AcmValidatedDomain", name, tags=tags, customize=customize)
        self._domain_name = domain_name
        self._region = region

    def _create_resources(self) -> AcmValidatedDomainResources:
        dns = context().dns
        if dns is None:
            raise DnsProviderNotConfiguredError(
                "DNS provider is not configured in the context. "
                "Please set up a DNS provider to use custom domains."
            )

        # Use ProviderStore for cross-region provider when needed
        cross_region_provider = None
        if self._region and self._region != context().aws.region:
            cross_region_provider = ProviderStore.aws_for_region(self._region)

        # 1 - Issue Certificate
        certificate = pulumi_aws.acm.Certificate(
            context().prefix(f"{self.name}-certificate"),
            **self._customizer(
                "certificate",
                {
                    "domain_name": self._domain_name,
                    "validation_method": "DNS",
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(provider=cross_region_provider),
        )

        # 2 - Validate Certificate with DNS PROVIDER
        first_option = certificate.domain_validation_options.apply(lambda options: options[0])
        validation_record = dns.create_caa_record(
            resource_name=context().prefix(f"{self.name}-certificate-validation-record"),
            name=first_option.apply(lambda opt: opt["resource_record_name"]),
            **self._customizer(
                "validation_record",
                {
                    "record_type": first_option.apply(lambda opt: opt["resource_record_type"]),
                    "content": first_option.apply(lambda opt: opt["resource_record_value"]),
                    "ttl": 1,
                },
            ),
        )

        # 3 - Wait for validation - use the validation record's FQDN to ensure it exists
        cert_validation = pulumi_aws.acm.CertificateValidation(
            context().prefix(f"{self.name}-certificate-validation"),
            **self._customizer(
                "cert_validation",
                {
                    "certificate_arn": certificate.arn,
                    "validation_record_fqdns": [validation_record.name],
                },
            ),
            opts=self._resource_opts(
                depends_on=[certificate, validation_record.pulumi_resource],
                provider=cross_region_provider,
            ),
        )

        self.register_outputs({"arn": certificate.arn})
        return AcmValidatedDomainResources(
            certificate=certificate,
            validation_record=validation_record,
            cert_validation=cert_validation,
        )
