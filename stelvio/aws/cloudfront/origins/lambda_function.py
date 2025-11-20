import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouterRouteOriginConfig
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontBridge
from stelvio.aws.cloudfront.origins.decorators import register_bridge
from stelvio.aws.function import Function
from stelvio.context import context


@register_bridge(Function)
class LambdaFunctionCloudfrontBridge(ComponentCloudfrontBridge):
    def __init__(self, idx: int, route: Route) -> None:
        super().__init__(idx, route)
        self.function = route.component_or_url

    def get_origin_config(self) -> RouterRouteOriginConfig:
        # Create a Lambda Function URL for the function
        function_url = pulumi_aws.lambda_.FunctionUrl(
            context().prefix(f"{self.function.name}-url"),
            function_name=self.function.resources.function.name,
            authorization_type="NONE",  # No auth for CloudFront access
            cors={
                "allow_credentials": False,
                "allow_headers": ["*"],
                "allow_methods": ["*"],
                "allow_origins": ["*"],
                "expose_headers": [],
                "max_age": 86400,
            },
        )

        # Extract domain from function URL (remove https:// and trailing /)
        function_domain = function_url.function_url.apply(
            lambda url: url.replace("https://", "").rstrip("/")
        )

        origin_args = pulumi_aws.cloudfront.DistributionOriginArgs(
            origin_id=self.function.resources.function.name,
            domain_name=function_domain,
            origin_path="",  # Lambda Function URLs don't need a path prefix
        )
        origin_dict = {
            "origin_id": origin_args.origin_id,
            "domain_name": origin_args.domain_name,
            "origin_path": origin_args.origin_path,
            # For Lambda Function URLs, we need to specify custom_origin_config
            "custom_origin_config": {
                "http_port": 443,
                "https_port": 443,
                "origin_protocol_policy": "https-only",
                "origin_ssl_protocols": ["TLSv1.2"],
            },
            # No origin_access_control_id needed for Lambda Function URLs
        }
        # For Lambda functions, we need to handle both exact path and subpaths
        # Use a pattern that matches both /simple and /simple/*
        if self.route.path_pattern.endswith("*"):
            path_pattern = self.route.path_pattern
        else:
            # Create a pattern that matches both the exact path and subpaths
            # CloudFront doesn't support multiple patterns, so we use the broader pattern
            path_pattern = f"{self.route.path_pattern}*"
        function_code = strip_path_pattern_function_js(self.route.path_pattern)
        cf_function = pulumi_aws.cloudfront.Function(
            context().prefix(f"{self.function.name}-uri-rewrite-{self.idx}"),
            runtime="cloudfront-js-2.0",
            code=function_code,
            comment=f"Strip {self.route.path_pattern} prefix for route {self.idx}",
            opts=pulumi.ResourceOptions(depends_on=[self.function.resources.function]),
        )
        cache_behavior = {
            "path_pattern": path_pattern,
            "allowed_methods": ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"],
            "cached_methods": ["GET", "HEAD"],
            "target_origin_id": origin_dict["origin_id"],
            "compress": True,
            "viewer_protocol_policy": "redirect-to-https",
            "forwarded_values": {
                "query_string": True,  # Lambda functions often use query parameters
                "cookies": {"forward": "none"},
            },
            "min_ttl": 0,
            "default_ttl": 0,  # Don't cache Lambda responses by default
            "max_ttl": 0,
            "function_associations": [
                {
                    "event_type": "viewer-request",
                    "function_arn": cf_function.arn,
                }
            ],
        }

        return RouterRouteOriginConfig(
            origin_access_controls=None,  # Lambda Function URLs don't need OAC
            origins=origin_dict,
            ordered_cache_behaviors=cache_behavior,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(self, distribution: pulumi_aws.cloudfront.Distribution) -> any:  # noqa: ARG002
        # Lambda Function URLs do not require an S3 Bucket Policy equivalent
        return None
