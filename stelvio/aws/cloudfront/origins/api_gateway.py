import json

import pulumi
import pulumi_aws

from stelvio.aws.api_gateway import Api
from stelvio.aws.cloudfront.dtos import CloudflareRouterRouteOriginConfig
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.s3.s3 import Bucket
from stelvio.component import Component
from stelvio.context import context


class ApiGatewayCloudfrontBridge:
    def __init__(self, idx: int, route: any) -> None:
        self.api = route.component
        self.idx = idx
        self.route = route

    @staticmethod
    def match(stlv_component: Component) -> bool:
        return isinstance(stlv_component, Api)

    def get_origin_config(self) -> CloudflareRouterRouteOriginConfig:
        oac = pulumi_aws.cloudfront.OriginAccessControl(
            context().prefix(f"{self.api.name}-oac-{self.idx}"),
            description=f"Origin Access Control for {self.api.name} route {self.idx}",
            origin_access_control_origin_type="api-gateway",
            signing_behavior="always",
            signing_protocol="sigv4",
            opts=pulumi.ResourceOptions(depends_on=[self.api.resources.rest_api]),
        )
        origin_args = pulumi_aws.cloudfront.DistributionOriginArgs(
            origin_id=self.api.resources.rest_api.id,
            domain_name=self.api.resources.rest_api.execution_arn.apply(
                lambda arn: f"{self.api.resources.rest_api.id}.execute-api.{context().aws.region}.amazonaws.com"
            )
        )
        origin_dict = {
            "origin_id": origin_args.origin_id,
            "domain_name": origin_args.domain_name,
            "origin_access_control_id": oac.id,
        }
        path_pattern = (
            f"{self.route.path_pattern}/*"
            if not self.route.path_pattern.endswith("*")
            else self.route.path_pattern
        )
        function_code = strip_path_pattern_function_js(self.route.path_pattern)
        cf_function = pulumi_aws.cloudfront.Function(
            context().prefix(f"{self.api.name}-uri-rewrite-{self.idx}"),
            runtime="cloudfront-js-2.0",
            code=function_code,
            comment=f"Strip {self.route.path_pattern} prefix for route {self.idx}",
            opts=pulumi.ResourceOptions(depends_on=[self.api.resources.rest_api]),
        )
        cache_behavior = {
            "path_pattern": path_pattern,
            "allowed_methods": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
            "cached_methods": ["GET", "HEAD"],
            # "target_origin_id": origins[idx]["origin_id"],
            "target_origin_id": origin_dict["origin_id"],
            "compress": True,
        }

        return CloudflareRouterRouteOriginConfig(
            origin_access_controls=oac,
            origins=origin_dict,
            ordered_cache_behaviors=cache_behavior,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(
        self, distribution: pulumi_aws.cloudfront.Distribution
    ) -> any:
        # API Gateway does not require an S3 Bucket Policy equivalent
        return None