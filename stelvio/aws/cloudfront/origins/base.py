from abc import ABC, abstractmethod

import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.component import Component
from stelvio.context import context

API_ALLOWED_METHODS = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
API_CACHED_METHODS = ["GET", "HEAD"]


class ComponentCloudfrontAdapter(ABC):
    component_class: type[Component] | None = None

    def __init__(
        self, idx: int, route: Route, resource_opts: pulumi.ResourceOptions | None = None
    ) -> None:
        self.idx = idx
        self.route = route
        self.resource_opts = resource_opts

    @classmethod
    def match(cls, stlv_component: Component) -> bool:
        return isinstance(stlv_component, cls.component_class)

    @abstractmethod
    def get_origin_config(self) -> RouteOriginConfig:
        pass

    @abstractmethod
    def get_access_policy(
        self, distribution: pulumi_aws.cloudfront.Distribution
    ) -> pulumi.Resource | None:
        pass

    def _api_origin_dict(
        self,
        origin_args: pulumi_aws.cloudfront.DistributionOriginArgs,
    ) -> dict:
        return {
            "origin_id": origin_args.origin_id,
            "domain_name": origin_args.domain_name,
            "origin_path": origin_args.origin_path,
            "custom_origin_config": {
                "http_port": 80,
                "https_port": 443,
                "origin_protocol_policy": "https-only",
                "origin_ssl_protocols": ["TLSv1.2"],
            },
        }

    def _api_cache_behavior(
        self,
        *,
        origin_id: pulumi.Input[str],
        cf_function: pulumi_aws.cloudfront.Function,
        forwarded_headers: list[str] | None = None,
    ) -> dict:
        forwarded_values = {
            "query_string": True,
            "cookies": {"forward": "none"},
        }
        if forwarded_headers is not None:
            forwarded_values["headers"] = forwarded_headers

        return {
            "path_pattern": self._path_pattern(),
            "allowed_methods": API_ALLOWED_METHODS,
            "cached_methods": API_CACHED_METHODS,
            "target_origin_id": origin_id,
            "compress": True,
            "viewer_protocol_policy": "redirect-to-https",
            "forwarded_values": forwarded_values,
            "min_ttl": 0,
            "default_ttl": 0,
            "max_ttl": 0,
            "function_associations": [
                {
                    "event_type": "viewer-request",
                    "function_arn": cf_function.arn,
                }
            ],
        }

    def _api_uri_rewrite_function(
        self,
        *,
        component_name: str,
        depends_on: list[pulumi.Resource],
    ) -> pulumi_aws.cloudfront.Function:
        return pulumi_aws.cloudfront.Function(
            context().prefix(f"{component_name}-uri-rewrite-{self.idx}"),
            runtime="cloudfront-js-2.0",
            code=strip_path_pattern_function_js(self.route.path_pattern),
            comment=f"Strip {self.route.path_pattern} prefix for route {self.idx}",
            opts=pulumi.ResourceOptions.merge(
                self.resource_opts,
                pulumi.ResourceOptions(depends_on=depends_on),
            ),
        )

    def _path_pattern(self) -> str:
        if self.route.path_pattern.endswith("*"):
            return self.route.path_pattern
        return f"{self.route.path_pattern}/*"
