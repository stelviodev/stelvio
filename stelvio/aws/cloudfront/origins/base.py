from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.component import Component, old_name_aliases, resource_name
from stelvio.context import context

API_ALLOWED_METHODS = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
API_CACHED_METHODS = ["GET", "HEAD"]
# AWS managed policies. Their IDs are the same in every account, so no lookup on each preview:
# https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html
# https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-origin-request-policies.html
CACHING_DISABLED_POLICY_ID = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"
# Every viewer header but Host: API Gateway picks the API by Host, a foreign one gets 403.
ALL_VIEWER_EXCEPT_HOST_HEADER_POLICY_ID = "b689b0a8-53d0-40ab-baf2-68738e2966ac"

# (customize key, computed props, default props) -> props: the owning Router's `_customizer`
type Customizer = Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]

# Short kinds leave room for long app and env names; the old words live on in the aliases.
_OLD_KINDS = {
    "rw": "uri-rewrite",
    "r": "lambda-edge-role",
    "p": "lambda-edge-policy",
    "edge": "host-rewrite",
    "perm": "cloudfront-permission",
}


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
        *,
        router_name: str,
    ) -> None:
        self.idx = idx
        self.route = route
        self.resource_opts = resource_opts
        self.customize = customize
        self.router_name = router_name

    @property
    def _stem(self) -> str:
        """Start of every per-route resource name. Keyed by component, not route position:
        adding or removing a route would otherwise rename, and replace, every shorter
        route's resources."""
        return f"{self.router_name}-{self.route.component.name}"

    @property
    def _old_stem(self) -> str:
        return self.route.component.name

    def _name(self, kind: str, *, limit: int | None) -> str:
        if limit is None:
            return context().prefix(f"{self._stem}-{kind}")
        return resource_name(self._stem, limit=limit, suffix=f"-{kind}")

    def _old_name(self, kind: str) -> str:
        # The name a deployed stack has from before the rename; `idx` lives only for this.
        return context().prefix(f"{self._old_stem}-{_OLD_KINDS.get(kind, kind)}-{self.idx}")

    def _opts(
        self,
        kind: str,
        *,
        depends_on: list[pulumi.Resource] | None = None,
        provider: pulumi.ProviderResource | None = None,
    ) -> pulumi.ResourceOptions:
        return pulumi.ResourceOptions.merge(
            self.resource_opts,
            pulumi.ResourceOptions(
                aliases=old_name_aliases(self._old_name(kind)),
                depends_on=depends_on,
                provider=provider,
            ),
        )

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
    ) -> list[dict]:
        return self._cache_behaviors(
            {
                "allowed_methods": API_ALLOWED_METHODS,
                "cached_methods": API_CACHED_METHODS,
                "target_origin_id": origin_id,
                "viewer_protocol_policy": "redirect-to-https",
                # Legacy `forwarded_values` drop `Authorization` on GET and every cookie.
                "cache_policy_id": CACHING_DISABLED_POLICY_ID,
                "origin_request_policy_id": ALL_VIEWER_EXCEPT_HOST_HEADER_POLICY_ID,
                "function_associations": self._function_associations(cf_function),
            }
        )

    def _cache_behaviors(self, behavior: dict) -> list[dict]:
        return [{"path_pattern": pattern, **behavior} for pattern in self.route.behavior_patterns]

    @staticmethod
    def _function_associations(cf_function: pulumi_aws.cloudfront.Function | None) -> list[dict]:
        if cf_function is None:
            return []
        return [{"event_type": "viewer-request", "function_arn": cf_function.arn}]

    def _uri_rewrite_function(
        self, *, depends_on: list[pulumi.Resource]
    ) -> pulumi_aws.cloudfront.Function | None:
        defaults = {
            "runtime": "cloudfront-js-2.0",
            "code": strip_path_pattern_function_js(self.route.path_pattern),
            # AWS caps a CloudFront Function comment at 128 chars
            "comment": f"Strip {self.route.path_pattern} prefix"[:128],
        }
        props = self.customize("cloudfront_functions", {}, defaults)
        # The root route has no prefix to strip, and its function would run on every request
        # that reaches the default behavior (billed per invocation). A wildcard route's
        # function compares the `*` literally and never matches a real path. Only the
        # generated code is skipped: a customized `code` is the user's own function (a guard,
        # a redirect) and keeps its association.
        generated = props.get("code") == defaults["code"]
        if (self.route.is_root or self.route.is_wildcard) and generated:
            return None
        return pulumi_aws.cloudfront.Function(
            self._name("rw", limit=64),
            **props,
            opts=self._opts("rw", depends_on=depends_on),
        )
