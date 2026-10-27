from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypedDict, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain, AcmValidatedDomainCustomizationDict
from stelvio.aws.api_gateway.validators import url_domain, validate_domain_name
from stelvio.aws.cloudfront.dtos import Route
from stelvio.aws.cloudfront.js import default_404_function_js
from stelvio.aws.cloudfront.origins.components.url import Url
from stelvio.aws.cloudfront.origins.registry import CloudfrontAdapterRegistry
from stelvio.aws.function import Function, FunctionUrlConfig, FunctionUrlConfigDict
from stelvio.aws.s3.s3 import Bucket
from stelvio.component import Component
from stelvio.dns import DnsProviderNotConfiguredError, Record
from stelvio.provider import ProviderStore

if TYPE_CHECKING:
    from pulumi_aws.cloudfront import DistributionArgs, FunctionArgs, OriginAccessControlArgs
    from pulumi_aws.s3 import BucketPolicyArgs

    from stelvio.aws.cloudfront.cloudfront import CloudfrontPriceClass
    from stelvio.customize import ChildCustomization, Customization, CustomizationNoArgs


@final
@dataclass(frozen=True)
class RouterResources:
    distribution: pulumi_aws.cloudfront.Distribution
    origin_access_controls: list[pulumi_aws.cloudfront.OriginAccessControl]
    access_policies: list[pulumi_aws.s3.BucketPolicy | pulumi_aws.lambda_.Permission]
    cloudfront_functions: list[pulumi_aws.cloudfront.Function]
    acm_validated_domain: AcmValidatedDomain | None
    record: Record | None


class RouterCustomizationDict(TypedDict, total=False):
    distribution: Customization[DistributionArgs]
    origin_access_controls: Customization[OriginAccessControlArgs]
    access_policies: Customization[BucketPolicyArgs]
    cloudfront_functions: Customization[FunctionArgs]
    acm_validated_domain: ChildCustomization[AcmValidatedDomainCustomizationDict]
    record: CustomizationNoArgs  # No specific Pulumi Args (cross cloud compat)


@final
class Router(Component[RouterResources, RouterCustomizationDict]):
    def __init__(  # noqa: PLR0913
        self,
        name: str,
        routes: list[Route] | None = None,
        price_class: CloudfrontPriceClass | None = None,
        custom_domain: str | None = None,
        *,
        tags: dict[str, str] | None = None,
        customize: RouterCustomizationDict | None = None,
    ):
        super().__init__(
            ProviderStore.aws(), "stelvio:aws:Router", name, tags=tags, customize=customize
        )
        # Checked like `route()` calls but not sorted: a new order would rename every resource
        # named by route index on a stack deployed with this one.
        self.routes: list[Route] = []
        for route in routes or []:
            self._add_route(route)
        self.price_class = price_class
        self.custom_domain = custom_domain
        # Truthy check: "" keeps meaning "no domain", e.g. `os.getenv("DOMAIN", "")`.
        if custom_domain:
            validate_domain_name(custom_domain, field_name="custom_domain", wildcard=True)

    def _create_resources(self) -> RouterResources:
        # Create ACM Validated Domain if custom domain is provided
        acm_validated_domain = None
        if self.custom_domain:
            if context().dns is None:
                raise DnsProviderNotConfiguredError("DNS not configured.")
            # CloudFront requires ACM certificates in us-east-1
            acm_validated_domain = AcmValidatedDomain(
                f"{self.name}-acm-validated-domain",
                domain_name=self.custom_domain,
                tags=self.tags,
                customize=self._customize.get("acm_validated_domain"),
                region="us-east-1",
                parent=self,
            )

        if not self.routes:
            raise ValueError(f"Router '{self.name}' must have at least one route.")

        adapters = [
            CloudfrontAdapterRegistry.get_adapter_for_component(route.component)(
                idx, route, self._resource_opts(), self._customizer
            )
            for idx, route in enumerate(self.routes)
        ]

        route_configs = [adapter.get_origin_config() for adapter in adapters]

        root_idx = next((idx for idx, route in enumerate(self.routes) if route.is_root), None)

        if root_idx is None:
            # Create a CloudFront Function to return 404 for unmatched routes (default behavior)
            default_404_function_code = default_404_function_js()

            default_404_function = pulumi_aws.cloudfront.Function(
                context().prefix(f"{self.name}-default-404"),
                **self._customizer(
                    "cloudfront_functions",
                    {},
                    {
                        "runtime": "cloudfront-js-2.0",
                        "code": default_404_function_code,
                        "comment": "Return 404 for unmatched routes",
                    },
                ),
                opts=self._resource_opts(),
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
                    # Clears a removed root route's headers (the provider keeps a missing one).
                    "headers": [],
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
            # The root route's own behavior, so it keeps its methods, forwarding and caching.
            [root_behavior] = route_configs[root_idx].ordered_cache_behaviors
            default_cache_behavior = {
                k: v for k, v in root_behavior.items() if k != "path_pattern"
            }

        ordered_cache_behaviors = [
            behavior
            for idx, rc in enumerate(route_configs)
            if idx != root_idx
            for behavior in rc.ordered_cache_behaviors
        ]

        root_is_bucket = root_idx is not None and isinstance(
            self.routes[root_idx].component, Bucket
        )
        distribution = pulumi_aws.cloudfront.Distribution(
            context().prefix(self.name),
            **self._customizer(
                "distribution",
                {
                    "aliases": [self.custom_domain] if self.custom_domain else None,
                    "origins": [rc.origins for rc in route_configs],
                    "default_cache_behavior": default_cache_behavior,
                    "ordered_cache_behaviors": ordered_cache_behaviors or None,
                    "price_class": self.price_class,
                    "viewer_certificate": {
                        "acm_certificate_arn": acm_validated_domain.resources.certificate.arn,
                        "ssl_support_method": "sni-only",
                        "minimum_protocol_version": "TLSv1.2_2021",
                    }
                    if self.custom_domain
                    else None,
                },
                {
                    "enabled": True,
                    "is_ipv6_enabled": True,
                    # `GET /` on a bucket is a ListBucket call; its policy allows GetObject: 403.
                    "default_root_object": "index.html" if root_is_bucket else None,
                    "price_class": "PriceClass_100",
                    "restrictions": {
                        "geo_restriction": {
                            "restriction_type": "none",
                        }
                    },
                    "viewer_certificate": {"cloudfront_default_certificate": True},
                },
                inject_tags=True,
            ),
            opts=self._resource_opts(),
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
                **self._customizer(
                    "record",
                    {},
                    {
                        "record_type": "CNAME",
                        "value": distribution.domain_name,
                        "ttl": 1,
                    },
                ),
                opts=self._resource_opts(),
            )

        domain = url_domain(self.custom_domain) or distribution.domain_name
        self.register_outputs({"url": pulumi.Output.concat("https://", domain)})

        return RouterResources(
            distribution=distribution,
            origin_access_controls=[
                rc.origin_access_controls
                for rc in route_configs
                if rc.origin_access_controls is not None
            ],
            access_policies=access_policies,
            cloudfront_functions=[
                rc.cloudfront_functions
                for rc in route_configs
                if rc.cloudfront_functions is not None
            ]
            + ([default_404_function] if default_404_function else []),
            acm_validated_domain=acm_validated_domain,
            record=record,
        )

    def _add_route(self, route: Route) -> None:
        # The prefix-strip function compares the path literally against `request.uri`: a path
        # without the leading '/', with a trailing '/' or a wildcard before the end never
        # matches it. `'` and `\` break its JS string; `"`, space and non-ASCII arrive
        # percent-encoded and never match. CloudFront drops empty, `.` and `..` segments from
        # the request path before matching, so a path with one never matches either.
        path = route.path_pattern
        if not re.fullmatch(r"(/[A-Za-z0-9_.~$@:+/-]*\*?)?", path) or (
            path != "/" and re.search(r"/\.{0,2}(?:/|$)", path)
        ):
            raise ValueError(
                f"Invalid route path {path!r}: start with '/', no trailing '/', no empty, '.' "
                "or '..' segment, '*' only at the end, other chars only letters, digits and "
                "_ . ~ - / $ @ : +. Use '/api' to send '/api/users' to the origin as '/users', "
                "or '/api/*' to keep '/api/users'."
            )
        for existing_route in self.routes:
            if existing_route.path_pattern == route.path_pattern:
                raise ValueError(f"Route for path pattern {route.path_pattern} already exists.")
            if existing_route.is_root and route.is_root:
                raise ValueError(
                    f"Route for path pattern {route.path_pattern!r} clashes with "
                    f"{existing_route.path_pattern!r}: both are the root route."
                )
            if existing_route.prefix_pattern == route.prefix_pattern:
                raise ValueError(
                    f"Route for path pattern {route.path_pattern!r} clashes with "
                    f"{existing_route.path_pattern!r}: both need the CloudFront behavior "
                    f"{route.prefix_pattern!r}."
                )
            if existing_route.component == route.component:
                raise ValueError(f"Route for origin {route.component} already exists.")

        if isinstance(route.component, str):
            url = route.component.strip()
            sha_url = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
            url_origin = Url(
                context().prefix(f"{self.name}-url-origin-{sha_url}"), url=url, tags=self.tags
            )
            route = replace(route, component=url_origin)
        elif not isinstance(route.component, Component):
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

    def route(
        self,
        path: str,
        component_or_url: Component | str,
        function_url: FunctionUrlConfig | FunctionUrlConfigDict | None = None,
    ) -> None:
        """Add a route to the router.

        Args:
            path: The path pattern to route (e.g. "/api", "/files").
            component_or_url: The component (Bucket, RestApi, Function) or URL string to route to.
            function_url: Function URL config (only used if component_or_url is a Function).
        """
        self._check_not_created("routes")
        self._add_route(Route(path, component_or_url, function_url))
        # Sort routes by path pattern length in descending order (more specific first)
        self.routes.sort(key=lambda r: len(r.path_pattern), reverse=True)
