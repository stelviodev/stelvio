from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.cloudfront.origins.s3 import S3BucketCloudfrontBridge
from stelvio.component import Component
from stelvio.dns import DnsProviderNotConfiguredError

if TYPE_CHECKING:
    from stelvio.aws.api_gateway.constants import HTTPMethodInput
    from stelvio.aws.cloudfront.cloudfront import CloudfrontPriceClass
    from stelvio.dns import Record


@final
class CloudfrontRoute:
    def __init__(self, path_pattern: str, component: Component):
        self.path_pattern = path_pattern
        self.component = component


@dataclass(frozen=True)
class CloudfrontRouterResources:
    distribution: pulumi_aws.cloudfront.Distribution
    origin_access_controls: list[pulumi_aws.cloudfront.OriginAccessControl]
    bucket_policies: list[pulumi_aws.s3.BucketPolicy]
    cloudfront_functions: list[pulumi_aws.cloudfront.Function]
    acm_validated_domain: AcmValidatedDomain | None
    record: Record | None


# def strip_path_pattern_function_js(path_pattern: str) -> str:
#     return f"""
#         function handler(event) {{
#             var request = event.request;
#             var uri = request.uri;
#             // Strip the path prefix '{path_pattern}'
#             if (uri.startsWith('{path_pattern}/')) {{
#                 request.uri = uri.substring({len(path_pattern)});
#             }}
#             return request;
#         }}
#         """.strip()


def default_404_function_js() -> str:
    return """
        function handler(event) {
            return {
                statusCode: 404,
                statusDescription: 'Not Found',
                headers: {
                    'content-type': { value: 'text/html' }
                },
                body: '<!DOCTYPE html><html><head><title>404 Not Found</title></head>'
                '<body><h1>404 Not Found</h1><p>The requested resource was not found.</p></body>'
                '</html>'
            };
        }
        """.strip()


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

        route_configs = []

        for idx, route in enumerate(self.routes):
            bridge = S3BucketCloudfrontBridge(route.component, idx, route)
            route_config = bridge.get_origin_config()
            route_configs.append(route_config)

        # Create a CloudFront Function to return 404 for unmatched routes (default behavior)
        default_404_function_code = default_404_function_js()

        default_404_function = pulumi_aws.cloudfront.Function(
            context().prefix(f"{self.name}-default-404"),
            runtime="cloudfront-js-2.0",
            code=default_404_function_code,
            comment="Return 404 for unmatched routes",
        )
        # cloudfront_functions.append(default_404_function)

        distribution = pulumi_aws.cloudfront.Distribution(
            context().prefix(self.name),
            aliases=[self.custom_domain] if self.custom_domain else None,
            origins=[rc.origins for rc in route_configs],
            enabled=True,
            is_ipv6_enabled=True,
            default_cache_behavior={
                "allowed_methods": ["GET", "HEAD", "OPTIONS"],
                "cached_methods": ["GET", "HEAD"],
                # Point to first origin, but the 404 function will intercept all requests
                # "target_origin_id": origins[0]["origin_id"] if origins else "default",
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
            },
            # ordered_cache_behaviors=ordered_cache_behaviors if ordered_cache_behaviors else None,
            ordered_cache_behaviors=[rc.ordered_cache_behaviors for rc in route_configs] or None,
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
        bucket_policies = []
        for idx, route in enumerate(self.routes):
            # Get the bucket from the component (assuming it's a Bucket component)
            if hasattr(route.component, "resources") and hasattr(
                route.component.resources, "bucket"
            ):
                bucket_policy = S3BucketCloudfrontBridge(
                    route.component, idx, route
                ).get_access_policy(distribution)
                bucket_policies.append(bucket_policy)

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
            # origin_access_controls=origin_access_controls,
            origin_access_controls=[rc.origin_access_controls for rc in route_configs],
            bucket_policies=bucket_policies,
            # cloudfront_functions=cloudfront_functions,
            cloudfront_functions=[rc.cloudfront_functions for rc in route_configs]
            + [default_404_function],
            acm_validated_domain=acm_validated_domain,
            record=record,
        )

    def _add_route(self, route: CloudfrontRoute) -> None:
        self.routes.append(route)

    def _remove_route(self, route: CloudfrontRoute) -> None:
        self.routes.remove(route)

    def route(
        self,
        http_method: HTTPMethodInput,  # noqa: ARG002
        path: str,
        component: Component,
    ) -> None:
        self._add_route(CloudfrontRoute(path, component))
