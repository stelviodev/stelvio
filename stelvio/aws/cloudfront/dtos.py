from dataclasses import dataclass
from typing import final

import pulumi_aws

from stelvio.component import Component


@dataclass(frozen=True)
class CloudfrontRouterRouteOriginConfig:
    origin_access_controls: pulumi_aws.cloudfront.OriginAccessControl | None
    origins: dict
    ordered_cache_behaviors: dict
    cloudfront_functions: pulumi_aws.cloudfront.Function


@final
class CloudfrontRoute:
    def __init__(self, path_pattern: str, component: Component):
        self.path_pattern = path_pattern
        self.component = component
