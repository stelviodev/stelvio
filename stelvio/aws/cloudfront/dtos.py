from dataclasses import dataclass
from typing import final

import pulumi_aws

from stelvio.aws.function import FunctionUrlConfig, FunctionUrlConfigDict
from stelvio.component import Component


@final
@dataclass(frozen=True)
class RouteOriginConfig:
    origin_access_controls: pulumi_aws.cloudfront.OriginAccessControl | None
    origins: dict
    ordered_cache_behaviors: list[dict]
    cloudfront_functions: pulumi_aws.cloudfront.Function | None


@final
@dataclass(frozen=True)
class Route:
    path_pattern: str
    component: Component | str
    function_url_config: FunctionUrlConfig | FunctionUrlConfigDict | None = None

    @property
    def is_root(self) -> bool:
        return self.path_pattern in ("", "/", "/*")

    @property
    def is_wildcard(self) -> bool:
        """A `*` anywhere makes the path a CloudFront pattern: used as typed, no prefix strip."""
        return "*" in self.path_pattern

    @property
    def prefix_pattern(self) -> str:
        """The cache behavior for everything under a plain path; a path with a `*` is its own
        pattern. Two routes sharing it would give the distribution one path pattern twice, which
        CloudFront rejects, so `Router` checks it when a route is added."""
        if self.is_wildcard:
            return self.path_pattern
        return f"{self.path_pattern}/*"

    @property
    def behavior_patterns(self) -> list[str]:
        """The route's cache behavior patterns, exact path first: `/api/*` alone sends `/api`
        to the default behavior. Root and wildcard routes have no exact path. S3 routes take
        only the last one: the exact path would ask the bucket for its root."""
        if self.is_root or self.is_wildcard:
            return [self.prefix_pattern]
        return [self.path_pattern, self.prefix_pattern]
