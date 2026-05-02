from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypedDict, final

import pulumi_aws

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.component import Component
from stelvio.dns import DnsProviderNotConfiguredError

if TYPE_CHECKING:
    import pulumi


@final
@dataclass(frozen=True)
class HttpApiDomainResources:
    acm_domain: AcmValidatedDomain
    custom_domain: pulumi_aws.apigatewayv2.DomainName


class HttpApiDomainCustomizationDict(TypedDict, total=False):
    certificate: pulumi_aws.acm.CertificateArgs | dict[str, Any] | None
    custom_domain: pulumi_aws.apigatewayv2.DomainNameArgs | dict[str, Any] | None
    dns_record: dict[str, Any] | None


@final
class HttpApiDomain(Component[HttpApiDomainResources, HttpApiDomainCustomizationDict]):
    """Standalone custom domain for HTTP API.

    Owns the ACM certificate, the apigatewayv2 DomainName resource, and the
    public DNS record. Multiple HttpApi instances can share one HttpApiDomain
    using distinct api_mapping_key values.
    """

    # Maps (domain_component_name, mapping_key) → HttpApi name — for conflict detection
    _registered_mappings: ClassVar[dict[tuple[str, str | None], str]] = {}

    _domain_name: str

    def __init__(
        self,
        name: str,
        *,
        domain_name: str,
        tags: dict[str, str] | None = None,
        customize: HttpApiDomainCustomizationDict | None = None,
        parent: pulumi.Resource | None = None,
    ) -> None:
        super().__init__(
            "stelvio:aws:HttpApiDomain", name, tags=tags, customize=customize, parent=parent
        )
        if not domain_name or not domain_name.strip():
            raise ValueError("domain_name cannot be empty")
        self._domain_name = domain_name

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
        key = (self.name, mapping_key)
        if key in HttpApiDomain._registered_mappings:
            existing = HttpApiDomain._registered_mappings[key]
            key_str = repr(mapping_key) if mapping_key else "(root)"
            raise ValueError(
                f"Duplicate api_mapping_key {key_str} for domain '{self._domain_name}': "
                f"already registered by HttpApi '{existing}', "
                f"cannot also register HttpApi '{api_name}'"
            )
        HttpApiDomain._registered_mappings[key] = api_name

    def _create_resources(self) -> HttpApiDomainResources:
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
            tags=self._tags or None,
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
                "custom_domain",
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
        dns.create_caa_record(
            resource_name=context().prefix(f"{self.name}-dns-record"),
            name=self._domain_name,
            **self._customizer(
                "dns_record",
                {
                    "record_type": "CNAME",
                    "content": custom_domain.domain_name_configuration.apply(
                        lambda cfg: cfg["target_domain_name"]
                    ),
                    "ttl": 300,
                },
            ),
        )

        return HttpApiDomainResources(
            acm_domain=acm_domain,
            custom_domain=custom_domain,
        )
