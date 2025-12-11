import pulumi
import pulumi_aws

from stelvio.aws.api_gateway import Api
from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter
from stelvio.aws.cloudfront.origins.decorators import register_adapter
from stelvio.context import context


@register_adapter(Api)
class ApiGatewayCloudfrontAdapter(ComponentCloudfrontAdapter):
    def __init__(self, idx: int, route: Route) -> None:
        super().__init__(idx, route)
        self.api = route.component

    def get_origin_config(self) -> RouteOriginConfig:
        # API Gateway doesn't need Origin Access Control like S3 buckets do
        # API Gateway has its own access control mechanisms
        region = pulumi_aws.get_region().name
        origin_args = pulumi_aws.cloudfront.DistributionOriginArgs(
            origin_id=self.api.resources.rest_api.id,
            domain_name=self.api.resources.rest_api.id.apply(
                lambda api_id: f"{api_id}.execute-api.{region}.amazonaws.com"
            ),
            # API Gateway needs the stage name in the origin path
            origin_path=self.api.resources.stage.stage_name.apply(lambda stage: f"/{stage}"),
        )
        origin_dict = {
            "origin_id": origin_args.origin_id,
            "domain_name": origin_args.domain_name,
            "origin_path": origin_args.origin_path,
            # For API Gateway, we need to specify custom_origin_config to avoid S3 validation
            "custom_origin_config": {
                "http_port": 80,
                "https_port": 443,
                "origin_protocol_policy": "https-only",
                "origin_ssl_protocols": ["TLSv1.2"],
            },
            # No origin_access_control_id needed for API Gateway
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
            "target_origin_id": origin_dict["origin_id"],
            "compress": True,
            "viewer_protocol_policy": "redirect-to-https",
            "forwarded_values": {
                "query_string": True,  # API Gateway often uses query parameters
                "cookies": {"forward": "none"},
            },
            "min_ttl": 0,
            "default_ttl": 0,  # Don't cache API responses by default
            "max_ttl": 0,
            "function_associations": [
                {
                    "event_type": "viewer-request",
                    "function_arn": cf_function.arn,
                }
            ],
        }

        return RouteOriginConfig(
            origin_access_controls=None,  # API Gateway doesn't need OAC
            origins=origin_dict,
            ordered_cache_behaviors=cache_behavior,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(self, distribution: pulumi_aws.cloudfront.Distribution) -> None:  # noqa: ARG002
        # API Gateway does not require an S3 Bucket Policy equivalent
        return None
