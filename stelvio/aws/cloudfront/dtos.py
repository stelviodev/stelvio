from dataclasses import dataclass
from typing import final

import pulumi_aws

from stelvio.component import Component


@dataclass(frozen=False)
class RouterRouteOriginConfig:
    origin_access_controls: pulumi_aws.cloudfront.OriginAccessControl | None
    origins: dict
    ordered_cache_behaviors: dict | None
    cloudfront_functions: pulumi_aws.cloudfront.Function


@final
class Route:
    def __init__(self, path_pattern: str, component_or_url: Component | str):
        self.path_pattern = path_pattern
        self.component_or_url = component_or_url
