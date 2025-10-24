from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.cloudfront.dtos import CloudfrontRoute
from stelvio.aws.cloudfront.js import default_404_function_js
from stelvio.aws.cloudfront.origins.registry import CloudfrontBridgeRegistry
from stelvio.component import Component
from stelvio.dns import DnsProviderNotConfiguredError

if TYPE_CHECKING:
    from stelvio.aws.cloudfront.cloudfront import CloudfrontPriceClass
    from stelvio.dns import Record


@dataclass(frozen=True)
class CloudfrontRouterResources:
    distribution: pulumi_aws.cloudfront.Distribution
    origin_access_controls: list[pulumi_aws.cloudfront.OriginAccessControl]
    access_policies: list[pulumi_aws.s3.BucketPolicy]
    cloudfront_functions: list[pulumi_aws.cloudfront.Function]
    acm_validated_domain: AcmValidatedDomain | None
    record: Record | None


@final
class CloudfrontRouter(Component[CloudfrontRouterResources]):
    def __init__(
        self,
        name: str,
        routes: list[CloudfrontRoute] | None = None,
        price_class: CloudfrontPriceClass = "PriceClass_100",
        custom_domain: str | None = None,
    ):
        super().__init__(name)
        self.routes = routes or []
        self.price_class = price_class
        self.custom_domain = custom_domain

    def _create_resources(self) -> CloudfrontRouterResources:
        # Create ACM Validated Domain if custom domain is provided
        acm_validated_domain = None
        if self.custom_domain:
            if context().dns is None:
                raise DnsProviderNotConfiguredError("DNS not configured.")
            acm_validated_domain = AcmValidatedDomain(
                f"{self.name}-acm-validated-domain",
                domain_name=self.custom_domain,
            )

        bridges = [
            CloudfrontBridgeRegistry.get_bridge_for_component(route.component)(idx, route)
            for idx, route in enumerate(self.routes)
        ]

        route_configs = [bridge.get_origin_config() for bridge in bridges]

        root_path_idx = None
        for idx, route in enumerate(self.routes):
            if route.path_pattern == "/":
                root_path_idx = idx
                break

        if root_path_idx is None:
            # Create a CloudFront Function to return 404 for unmatched routes (default behavior)
            default_404_function_code = default_404_function_js()

            default_404_function = pulumi_aws.cloudfront.Function(
                context().prefix(f"{self.name}-default-404"),
                runtime="cloudfront-js-2.0",
                code=default_404_function_code,
                comment="Return 404 for unmatched routes",
            )

            default_cache_behavior = {
                "allowed_methods": ["GET", "HEAD", "OPTIONS"],
                "cached_methods": ["GET", "HEAD"],
                # Point to first origin, but the 404 function will intercept all requests
                "target_origin_id": route_configs[0].origins["origin_id"]
                if route_configs
                else "default",
                "compress": True,
                "viewer_protocol_policy": "redirect-to-https",
                "forwarded_values": {
                    "query_string": False,
                    "cookies": {"forward": "none"},
                },
                "min_ttl": 0,
                "default_ttl": 0,  # Don't cache 404 responses
                "max_ttl": 0,
                "function_associations": [
                    {
                        "event_type": "viewer-request",
                        "function_arn": default_404_function.arn,
                    }
                ],
            }

        else:
            default_404_function = None

            default_cache_behavior = {
                "allowed_methods": ["GET", "HEAD", "OPTIONS"],
                "cached_methods": ["GET", "HEAD"],
                # Point to first origin, but the 404 function will intercept all requests
                "target_origin_id": route_configs[root_path_idx].origins["origin_id"],
                "compress": True,
                "viewer_protocol_policy": "redirect-to-https",
                "forwarded_values": {
                    "query_string": False,
                    "cookies": {"forward": "none"},
                },
                "min_ttl": 0,
                "default_ttl": 0,  # Don't cache 404 responses
                "max_ttl": 0,
                "function_associations": [
                    # {
                    #     "event_type": "viewer-request",
                    #     "function_arn": default_404_function.arn,
                    # }
                ],
            }
            # Remove the root path from ordered cache behaviors
            # route_configs[root_path_idx].ordered_cache_behaviors = None

        distribution = pulumi_aws.cloudfront.Distribution(
            context().prefix(self.name),
            aliases=[self.custom_domain] if self.custom_domain else None,
            origins=[rc.origins for rc in route_configs],
            enabled=True,
            is_ipv6_enabled=True,
            default_cache_behavior=default_cache_behavior,
            ordered_cache_behaviors=[
                rc.ordered_cache_behaviors for rc in route_configs if rc.ordered_cache_behaviors
            ]
            or None,
            price_class=self.price_class,
            restrictions={
                "geo_restriction": {
                    "restriction_type": "none",
                }
            },
            viewer_certificate={
                "acm_certificate_arn": acm_validated_domain.resources.certificate.arn,
                "ssl_support_method": "sni-only",
                "minimum_protocol_version": "TLSv1.2_2021",
            }
            if self.custom_domain
            else {
                "cloudfront_default_certificate": True,
            },
        )

        # Create bucket policies to allow CloudFront access for each S3 bucket
        access_policies = [
            policy
            for policy in [bridge.get_access_policy(distribution) for bridge in bridges]
            if policy is not None
        ]

        record = None
        if self.custom_domain:
            record = context().dns.create_record(
                resource_name=context().prefix(f"{self.name}-cloudfront-record"),
                name=self.custom_domain,
                record_type="CNAME",
                value=distribution.domain_name,
                ttl=1,
            )

        pulumi.export(f"cloudfront_{self.name}_domain_name", distribution.domain_name)
        pulumi.export(f"cloudfront_{self.name}_distribution_id", distribution.id)
        pulumi.export("num_origins", len(route_configs))

        return CloudfrontRouterResources(
            distribution=distribution,
            origin_access_controls=[
                rc.origin_access_controls
                for rc in route_configs
                if rc.origin_access_controls is not None
            ],
            access_policies=access_policies,
            cloudfront_functions=[rc.cloudfront_functions for rc in route_configs]
            + [default_404_function],
            acm_validated_domain=acm_validated_domain,
            record=record,
        )

    def _add_route(self, route: CloudfrontRoute) -> None:
        for existing_route in self.routes:
            if existing_route.path_pattern == route.path_pattern:
                raise ValueError(f"Route for path pattern {route.path_pattern} already exists.")
        self.routes.append(route)

    def route(
        self,
        path: str,
        component: Component,
    ) -> None:
        self._add_route(CloudfrontRoute(path, component))
