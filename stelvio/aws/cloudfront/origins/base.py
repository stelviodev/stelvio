from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.component import Component
from stelvio.context import context

API_ALLOWED_METHODS = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
API_CACHED_METHODS = ["GET", "HEAD"]

# (customize key, computed props, default props) -> props: the owning Router's `_customizer`
type Customizer = Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]


def _as_is(_key: str, props: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    return defaults | {k: v for k, v in props.items() if v is not None}


class ComponentCloudfrontAdapter(ABC):
    component_class: type[Component] | None = None

    def __init__(
        self,
        idx: int,
        route: Route,
        resource_opts: pulumi.ResourceOptions | None = None,
        customize: Customizer = _as_is,
    ) -> None:
        self.idx = idx
        self.route = route
        self.resource_opts = resource_opts
        self.customize = customize

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

    def _api_cache_behaviors(
        self,
        *,
        origin_id: pulumi.Input[str],
        cf_function: pulumi_aws.cloudfront.Function | None,
        forwarded_headers: list[str] | None = None,
    ) -> list[dict]:
        forwarded_values = {
            "query_string": True,
            "cookies": {"forward": "none"},
        }
        if forwarded_headers is not None:
            forwarded_values["headers"] = forwarded_headers

        return self._cache_behaviors(
            {
                "allowed_methods": API_ALLOWED_METHODS,
                "cached_methods": API_CACHED_METHODS,
                "target_origin_id": origin_id,
                "compress": True,
                "viewer_protocol_policy": "redirect-to-https",
                "forwarded_values": forwarded_values,
                "min_ttl": 0,
                "default_ttl": 0,
                "max_ttl": 0,
                "function_associations": self._function_associations(cf_function),
            }
        )

    def _cache_behaviors(self, behavior: dict) -> list[dict]:
        """`behavior` for the exact path and for everything under it: `/api/*` alone sends
        `/api` to the default behavior. Root and wildcard routes have no exact path."""
        patterns = [self._path_pattern()]
        if not (self.route.is_root or self.route.path_pattern.endswith("*")):
            patterns.insert(0, self.route.path_pattern)
        return [{"path_pattern": pattern, **behavior} for pattern in patterns]

    @staticmethod
    def _function_associations(cf_function: pulumi_aws.cloudfront.Function | None) -> list[dict]:
        if cf_function is None:
            return []
        return [{"event_type": "viewer-request", "function_arn": cf_function.arn}]

    def _uri_rewrite_function(
        self,
        *,
        component_name: str,
        depends_on: list[pulumi.Resource],
    ) -> pulumi_aws.cloudfront.Function | None:
        # The root route has no prefix to strip, and its function would run on every request
        # that reaches the default behavior (billed per invocation). A wildcard route's
        # function compares the `*` literally and never matches a real path.
        if self.route.is_root or self.route.path_pattern.endswith("*"):
            return None
        return pulumi_aws.cloudfront.Function(
            context().prefix(f"{component_name}-uri-rewrite-{self.idx}"),
            **self.customize(
                "cloudfront_functions",
                {},
                {
                    "runtime": "cloudfront-js-2.0",
                    "code": strip_path_pattern_function_js(self.route.path_pattern),
                    "comment": f"Strip {self.route.path_pattern} prefix for route {self.idx}",
                },
            ),
            opts=pulumi.ResourceOptions.merge(
                self.resource_opts,
                pulumi.ResourceOptions(depends_on=depends_on),
            ),
        )

    def _path_pattern(self) -> str:
        if self.route.path_pattern.endswith("*"):
            return self.route.path_pattern
        return f"{self.route.path_pattern}/*"
