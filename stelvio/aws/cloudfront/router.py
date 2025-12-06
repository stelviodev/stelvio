import hashlib
from dataclasses import dataclass
from typing import final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.cloudfront.cloudfront import CloudfrontPriceClass
from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.js import default_404_function_js
from stelvio.aws.cloudfront.origins.components.url import Url
from stelvio.aws.cloudfront.origins.registry import CloudfrontAdapterRegistry
from stelvio.aws.function import FunctionUrlConfig, FunctionUrlConfigDict
from stelvio.component import Component
from stelvio.dns import DnsProviderNotConfiguredError, Record


@dataclass(frozen=True)
class RouterResources:
    distribution: pulumi_aws.cloudfront.Distribution
    origin_access_controls: list[pulumi_aws.cloudfront.OriginAccessControl]
    access_policies: list[pulumi_aws.s3.BucketPolicy]
    cloudfront_functions: list[pulumi_aws.cloudfront.Function]
    acm_validated_domain: AcmValidatedDomain | None
    record: Record | None


@final
class Router(Component[RouterResources]):
    def __init__(
        self,
        name: str,
        routes: list[Route] | None = None,
        price_class: CloudfrontPriceClass = "PriceClass_100",
        custom_domain: str | None = None,
    ):
        super().__init__(name)
        self.routes = routes or []
        self.price_class = price_class
        self.custom_domain = custom_domain

    def _create_resources(self) -> RouterResources:
        # Create ACM Validated Domain if custom domain is provided
        acm_validated_domain = None
        if self.custom_domain:
            if context().dns is None:
                raise DnsProviderNotConfiguredError("DNS not configured.")
            acm_validated_domain = AcmValidatedDomain(
                f"{self.name}-acm-validated-domain",
                domain_name=self.custom_domain,
            )

        if not self.routes:
            raise ValueError(f"Router '{self.name}' must have at least one route.")

        adapters = [
            CloudfrontAdapterRegistry.get_adapter_for_component(route.component)(idx, route)
            for idx, route in enumerate(self.routes)
        ]

        route_configs = [adapter.get_origin_config() for adapter in adapters]

        root_path_idx = next(
            (idx for idx, route in enumerate(self.routes) if route.path_pattern == "/"), None
        )

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
                # Use the root path origin for default cache behavior
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
                "function_associations": [],
            }

        ordered_cache_behaviors = []
        for rc in route_configs:
            if rc.ordered_cache_behaviors:
                if isinstance(rc.ordered_cache_behaviors, list):
                    ordered_cache_behaviors.extend(rc.ordered_cache_behaviors)
                else:
                    ordered_cache_behaviors.append(rc.ordered_cache_behaviors)

        distribution = pulumi_aws.cloudfront.Distribution(
            context().prefix(self.name),
            aliases=[self.custom_domain] if self.custom_domain else None,
            origins=[rc.origins for rc in route_configs],
            enabled=True,
            is_ipv6_enabled=True,
            default_cache_behavior=default_cache_behavior,
            ordered_cache_behaviors=ordered_cache_behaviors or None,
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
            for policy in [adapter.get_access_policy(distribution) for adapter in adapters]
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

        pulumi.export(f"router_{self.name}_domain_name", distribution.domain_name)
        pulumi.export(f"router_{self.name}_distribution_id", distribution.id)
        pulumi.export(f"router_{self.name}_num_origins", len(route_configs))

        return RouterResources(
            distribution=distribution,
            origin_access_controls=[
                rc.origin_access_controls
                for rc in route_configs
                if rc.origin_access_controls is not None
            ],
            access_policies=access_policies,
            cloudfront_functions=[rc.cloudfront_functions for rc in route_configs]
            + ([default_404_function] if default_404_function else []),
            acm_validated_domain=acm_validated_domain,
            record=record,
        )

    def _add_route(self, route: Route) -> None:
        from stelvio.aws.function import Function

        for existing_route in self.routes:
            if existing_route.path_pattern == route.path_pattern:
                raise ValueError(f"Route for path pattern {route.path_pattern} already exists.")
            if existing_route.component == route.component:
                raise ValueError(f"Route for origin {route.component} already exists.")

        if not isinstance(route.component, Component | str):
            raise TypeError(
                f"component_or_url must be a Component or str, got "
                f"{type(route.component).__name__}."
            )

        # Validate that Functions with 'url' config cannot be added to Router
        if isinstance(route.component, Function) and route.component.config.url is not None:
            raise ValueError(
                f"Function '{route.component.name}' has 'url' configuration and cannot be "
                f"added to Router. Functions with 'url' config are standalone and should be "
                f"accessed directly via their Function URL, not through CloudFront Router. "
                f"Remove the 'url' parameter from the Function to make it Router-compatible."
            )
        self.routes.append(route)
        # Sort routes by path pattern length in descending order (more specific first)
        self.routes.sort(key=lambda r: len(r.path_pattern), reverse=True)

    def route(
        self,
        path: str,
        component_or_url: Component | str,
        function_url: FunctionUrlConfig | FunctionUrlConfigDict | None = None,
    ) -> None:
        """Add a route to the router.

        Args:
            path: The path pattern to route (e.g. "/api", "/files").
            component_or_url: The component (Bucket, Api, Function) or URL string to route to.
            function_url: Function URL config (only used if component_or_url is a Function).
        """
        if isinstance(component_or_url, str):
            url = component_or_url.strip()
            sha_url = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
            component_or_url = Url(
                context().prefix(f"{self.name}-url-origin-{sha_url}"),
                url=url,
            )
        self._add_route(Route(path, component_or_url, function_url))
