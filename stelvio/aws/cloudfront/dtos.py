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
    ordered_cache_behaviors: dict | list[dict] | None
    cloudfront_functions: pulumi_aws.cloudfront.Function


@final
@dataclass(frozen=True)
class Route:
    path_pattern: str
    component: Component | str
    function_url_config: FunctionUrlConfig | FunctionUrlConfigDict | None = None
