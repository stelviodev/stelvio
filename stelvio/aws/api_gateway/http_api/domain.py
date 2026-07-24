from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, final

import pulumi_aws

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway.validators import validate_domain_name
from stelvio.component import Component
from stelvio.dns import DnsProviderNotConfiguredError, Record

if TYPE_CHECKING:
    import pulumi


@final
@dataclass(frozen=True)
class ApiDomainResources:
    acm_domain: AcmValidatedDomain
    custom_domain: pulumi_aws.apigatewayv2.DomainName
    dns_record: Record


class ApiDomainCustomizationDict(TypedDict, total=False):
    certificate: pulumi_aws.acm.CertificateArgs | dict[str, Any] | None
    domain: pulumi_aws.apigatewayv2.DomainNameArgs | dict[str, Any] | None
    dns_record: dict[str, Any] | None


@final
class ApiDomain(Component[ApiDomainResources, ApiDomainCustomizationDict]):
    """Standalone custom domain for HTTP API.

    Owns the ACM certificate, the apigatewayv2 DomainName resource, and the
    public DNS record. Multiple HttpApi instances can share one ApiDomain
    using distinct api_mapping_key values.
    """

    _domain_name: str
    _registered_mappings: dict[str | None, str]

    def __init__(
        self,
        name: str,
        *,
        domain_name: str,
        tags: dict[str, str] | None = None,
        customize: ApiDomainCustomizationDict | None = None,
        parent: pulumi.Resource | None = None,
    ) -> None:
        super().__init__(
            "stelvio:aws:HttpApiDomain", name, tags=tags, customize=customize, parent=parent
        )
        validate_domain_name(domain_name)
        self._domain_name = domain_name
        self._registered_mappings = {}

    @property
    def domain_name(self) -> str:
        return self._domain_name

    @property
    def arn(self) -> pulumi.Output[str]:
        return self.resources.custom_domain.arn

    @property
    def target_domain_name(self) -> pulumi.Output[str]:
        return self.resources.custom_domain.domain_name_configuration.apply(
            lambda cfg: cfg["target_domain_name"]
        )

    def register_mapping(self, api_name: str, mapping_key: str | None) -> None:
        """Register an ApiMapping key against this domain. Raises on duplicate."""
        if mapping_key in self._registered_mappings:
            existing = self._registered_mappings[mapping_key]
            key_str = repr(mapping_key) if mapping_key else "(root)"
            raise ValueError(
                f"Duplicate api_mapping_key {key_str} for domain '{self._domain_name}': "
                f"already registered by HttpApi '{existing}', "
                f"cannot also register HttpApi '{api_name}'"
            )
        self._registered_mappings[mapping_key] = api_name

    def _create_resources(self) -> ApiDomainResources:
        dns = context().dns
        if dns is None:
            raise DnsProviderNotConfiguredError(
                "DNS provider is not configured. "
                "Please set up a DNS provider to use custom domains."
            )

        # 1. Create ACM certificate + DNS validation record
        acm_domain = AcmValidatedDomain(
            f"{self.name}-cert",
            self._domain_name,
            tags=self._tags,
            customize={
                "certificate": (self._customize or {}).get("certificate"),
                "cert_validation": None,
            },
            parent=self,
        )

        # 2. Create API Gateway v2 DomainName resource
        custom_domain = pulumi_aws.apigatewayv2.DomainName(
            context().prefix(f"{self.name}-domain"),
            **self._customizer(
                "domain",
                {
                    "domain_name": self._domain_name,
                    "domain_name_configuration": {
                        "certificate_arn": acm_domain.resources.cert_validation.certificate_arn,
                        "endpoint_type": "REGIONAL",
                        "security_policy": "TLS_1_2",
                    },
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(depends_on=[acm_domain.resources.cert_validation]),
        )

        # 3. Create DNS CNAME/alias record pointing to the API Gateway regional domain
        dns_record = dns.create_record(
            resource_name=context().prefix(f"{self.name}-dns-record"),
            name=self._domain_name,
            **self._customizer(
                "dns_record",
                {
                    "record_type": "CNAME",
                    "value": custom_domain.domain_name_configuration.apply(
                        lambda cfg: cfg["target_domain_name"]
                    ),
                    "ttl": 300,
                },
            ),
        )

        return ApiDomainResources(
            acm_domain=acm_domain,
            custom_domain=custom_domain,
            dns_record=dns_record,
        )
