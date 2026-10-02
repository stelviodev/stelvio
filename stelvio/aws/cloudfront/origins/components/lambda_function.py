import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.origins.base import (
    ComponentCloudfrontAdapter,
    Customizer,
    _as_is,
)
from stelvio.aws.cloudfront.origins.registry import register_adapter
from stelvio.aws.function import Function, FunctionUrlConfig
from stelvio.aws.function.config import FunctionUrlConfigDict
from stelvio.aws.function.function import _create_function_url
from stelvio.component import old_name_aliases, parse_config, resource_name
from stelvio.context import context


@register_adapter(Function)
class LambdaFunctionCloudfrontAdapter(ComponentCloudfrontAdapter):
    def __init__(
        self,
        idx: int,
        route: Route,
        resource_opts: pulumi.ResourceOptions | None = None,
        customize: Customizer = _as_is,
    ) -> None:
        super().__init__(idx, route, resource_opts, customize)
        self.function = route.component

    def get_origin_config(self) -> RouteOriginConfig:
        # Normalize function URL configuration
        url_config = _default_url_config(self.route.function_url_config)

        # Determine authorization type
        # auth='iam' → 'AWS_IAM', auth=None → 'NONE'
        auth_type = "AWS_IAM" if url_config.auth == "iam" else "NONE"

        name = f"{self.function.name}-router-{self.idx}"
        # This name used to carry the app prefix twice; the aliases keep deployed urls in place.
        old_name = resource_name(context().prefix(name), limit=64, suffix="-url")
        function_url = _create_function_url(
            name,
            self.function.resources.function,
            url_config,
            pulumi.ResourceOptions.merge(
                self.resource_opts, pulumi.ResourceOptions(aliases=old_name_aliases(old_name))
            ),
        )

        # Create OAC if using IAM authentication (secure by default)
        oac = None
        if auth_type == "AWS_IAM":
            oac = pulumi_aws.cloudfront.OriginAccessControl(
                context().prefix(f"{self.function.name}-oac-{self.idx}"),
                **self.customize(
                    "origin_access_controls",
                    {
                        "description": (
                            f"OAC for Lambda Function {self.function.name} route {self.idx}"
                        ),
                        "origin_access_control_origin_type": "lambda",
                        "signing_behavior": "always",
                        "signing_protocol": "sigv4",
                    },
                ),
                opts=pulumi.ResourceOptions.merge(
                    self.resource_opts,
                    pulumi.ResourceOptions(depends_on=[self.function.resources.function]),
                ),
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
                "http_port": 80,
                "https_port": 443,
                "origin_protocol_policy": "https-only",
                "origin_ssl_protocols": ["TLSv1.2"],
            },
        }

        # Add OAC if using IAM auth
        if oac is not None:
            origin_dict["origin_access_control_id"] = oac.id

        cf_function = self._uri_rewrite_function(
            component_name=self.function.name, depends_on=[self.function.resources.function]
        )

        cache_behavior_template = {
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

        if self.route.path_pattern.endswith("*"):
            cache_behavior = cache_behavior_template.copy()
            cache_behavior["path_pattern"] = self.route.path_pattern
            ordered_cache_behaviors = cache_behavior
        else:
            cb1 = cache_behavior_template.copy()
            cb1["path_pattern"] = self.route.path_pattern

            cb2 = cache_behavior_template.copy()
            cb2["path_pattern"] = f"{self.route.path_pattern}/*"

            ordered_cache_behaviors = [cb1, cb2]

        return RouteOriginConfig(
            origin_access_controls=oac,
            origins=origin_dict,
            ordered_cache_behaviors=ordered_cache_behaviors,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(
        self, distribution: pulumi_aws.cloudfront.Distribution
    ) -> pulumi_aws.lambda_.Permission | None:
        """Create Lambda Permission to allow CloudFront service principal to invoke the function.

        This is required when using OAC with IAM authentication.
        """
        # Only create permission if using IAM auth (OAC enabled)
        url_config = _default_url_config(self.route.function_url_config)

        if url_config.auth != "iam":
            return None

        # Grant cloudfront.amazonaws.com permission to invoke via Function URL
        return pulumi_aws.lambda_.Permission(
            context().prefix(f"{self.function.name}-cloudfront-permission-{self.idx}"),
            action="lambda:InvokeFunctionUrl",
            function=self.function.resources.function.name,
            principal="cloudfront.amazonaws.com",
            source_arn=distribution.arn,
            function_url_auth_type="AWS_IAM",
            opts=self.resource_opts,
        )


def _default_url_config(
    url_config: FunctionUrlConfig | FunctionUrlConfigDict | None,
) -> FunctionUrlConfig:
    """Return default FunctionUrlConfig."""
    url_config = parse_config(FunctionUrlConfig, url_config, {})
    # Explicitly handle 'default' auth to 'iam' for Router context
    if url_config.auth == "default":
        url_config = FunctionUrlConfig(
            auth="iam", cors=url_config.cors, streaming=url_config.streaming
        )
    return url_config
