import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouterRouteOriginConfig
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontBridge
from stelvio.aws.cloudfront.origins.decorators import register_bridge
from stelvio.aws.function import Function, FunctionUrlConfig
from stelvio.aws.function.config import FunctionUrlConfigDict
from stelvio.aws.function.function import _create_function_url
from stelvio.context import context


@register_bridge(Function)
class LambdaFunctionCloudfrontBridge(ComponentCloudfrontBridge):
    def __init__(self, idx: int, route: Route) -> None:
        super().__init__(idx, route)
        self.function = route.component_or_url

    def get_origin_config(self) -> RouterRouteOriginConfig:
        # Normalize function URL configuration
        url_config = _normalize_function_url_config(self.route.function_url_config)

        # Explicitly handle 'default' auth to 'iam' for Router context
        if url_config.auth == "default":
            url_config = FunctionUrlConfig(
                auth="iam", cors=url_config.cors, streaming=url_config.streaming
            )

        # Determine authorization type
        # auth='iam' → 'AWS_IAM', auth=None → 'NONE'
        auth_type = "AWS_IAM" if url_config.auth == "iam" else "NONE"

        function_url = _create_function_url(
            context().prefix(f"{self.function.name}-router-{self.idx}"),
            self.function.resources.function,
            url_config,
        )

        # Create OAC if using IAM authentication (secure by default)
        oac = None
        if auth_type == "AWS_IAM":
            oac = pulumi_aws.cloudfront.OriginAccessControl(
                context().prefix(f"{self.function.name}-oac-{self.idx}"),
                description=f"OAC for Lambda Function {self.function.name} route {self.idx}",
                origin_access_control_origin_type="lambda",
                signing_behavior="always",
                signing_protocol="sigv4",
                opts=pulumi.ResourceOptions(depends_on=[self.function.resources.function]),
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
        }

        # Add OAC if using IAM auth
        if oac is not None:
            origin_dict["origin_access_control_id"] = oac.id

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
            # Don't cache Lambda responses by default
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

        return RouterRouteOriginConfig(
            origin_access_controls=oac,
            origins=origin_dict,
            ordered_cache_behaviors=cache_behavior,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(
        self, distribution: pulumi_aws.cloudfront.Distribution
    ) -> pulumi_aws.lambda_.Permission | None:
        """Create Lambda Permission to allow CloudFront service principal to invoke the function.

        This is required when using OAC with IAM authentication.
        """
        # Only create permission if using IAM auth (OAC enabled)
        url_config = _normalize_function_url_config(self.route.function_url_config)

        # Explicitly handle 'default' auth to 'iam' for Router context
        if url_config.auth == "default":
            url_config = FunctionUrlConfig(
                auth="iam", cors=url_config.cors, streaming=url_config.streaming
            )

        if url_config.auth != "iam":
            return None

        # Grant cloudfront.amazonaws.com permission to invoke via Function URL
        return pulumi_aws.lambda_.Permission(
            context().prefix(f"{self.function.name}-cloudfront-permission-{self.idx}"),
            action="lambda:InvokeFunctionUrl",
            function=self.function.resources.function.name,
            principal="cloudfront.amazonaws.com",
            source_arn=distribution.arn,
        )


def _normalize_function_url_config(
    config: FunctionUrlConfig | FunctionUrlConfigDict | None,
) -> FunctionUrlConfig:
    """Normalize function_url configuration to FunctionUrlConfig."""

    if config is None:
        # Default: secure IAM auth, no CORS (CloudFront handles CORS if needed)
        return FunctionUrlConfig(auth="default", cors=None, streaming=False)
    if isinstance(config, FunctionUrlConfig):
        return config
    if isinstance(config, dict):
        return FunctionUrlConfig(**config)
    raise TypeError(f"Invalid function_url config type: {type(config).__name__}")
