from dataclasses import dataclass
from typing import final
from urllib.parse import urlparse

import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.js import set_custom_host_header, strip_path_pattern_function_js
from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter
from stelvio.aws.cloudfront.origins.decorators import register_adapter
from stelvio.component import Component
from stelvio.context import context
from stelvio.link import Linkable


@final
@dataclass(frozen=True)
class UrlResources:
    url: str


@final
class Url(Component[UrlResources], Linkable):
    def __init__(self, name: str, url: str):
        super().__init__(name)
        self._validate_url(url)
        self.url = url

    @staticmethod
    def _validate_url(url: str) -> None:
        """Validate that the URL is a valid HTTP or HTTPS URL."""
        if not url:
            raise ValueError("URL cannot be empty")

        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Invalid URL scheme '{parsed.scheme}'. "
                "Only 'http://' and 'https://' URLs are supported."
            )
        if not parsed.netloc:
            raise ValueError("URL must include a domain (e.g., 'https://example.com')")

    def _create_resources(self) -> UrlResources:
        return UrlResources(
            url=self.url,
        )


@register_adapter(Url)
class UrlCloudfrontAdapter(ComponentCloudfrontAdapter):
    def __init__(self, idx: int, route: Route) -> None:
        super().__init__(idx, route)
        self.url = route.component

    def get_origin_config(self) -> RouteOriginConfig:
        parsed = urlparse(self.url.resources.url)

        origin_id = context().prefix(f"url-origin-{self.idx}")

        origin_args = pulumi_aws.cloudfront.DistributionOriginArgs(
            origin_id=origin_id,
            domain_name=parsed.netloc,
            origin_path=parsed.path if parsed.path and parsed.path != "/" else None,
        )

        origin_dict: dict[str, pulumi.Output | str | None] = {
            "origin_id": origin_args.origin_id,
            "domain_name": origin_args.domain_name,
            "origin_path": origin_args.origin_path,
            "custom_origin_config": {
                "http_port": 80,
                "https_port": 443,
                "origin_protocol_policy": "https-only"
                if parsed.scheme == "https"
                else "http-only",
                "origin_ssl_protocols": ["TLSv1.2"],
            },
        }

        path_pattern = (
            f"{self.route.path_pattern}/*"
            if self.route.path_pattern and not self.route.path_pattern.endswith("*")
            else self.route.path_pattern or "/"
        )

        # CloudFront Function for path rewriting (viewer-request)
        function_code = strip_path_pattern_function_js(self.route.path_pattern or "/")
        cf_function = pulumi_aws.cloudfront.Function(
            context().prefix(f"url-origin-uri-rewrite-{self.idx}"),
            runtime="cloudfront-js-2.0",
            code=function_code,
            comment=f"Strip {self.route.path_pattern or '/'} prefix for URL route {self.idx}",
        )

        # Lambda@Edge for Host header rewriting (origin-request)
        # Create IAM role for Lambda@Edge
        lambda_role = pulumi_aws.iam.Role(
            context().prefix(f"url-origin-lambda-edge-role-{self.idx}"),
            assume_role_policy=pulumi.Output.json_dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {
                                "Service": ["lambda.amazonaws.com", "edgelambda.amazonaws.com"]
                            },
                            "Action": "sts:AssumeRole",
                        }
                    ],
                }
            ),
        )

        # Attach basic execution policy
        pulumi_aws.iam.RolePolicyAttachment(
            context().prefix(f"url-origin-lambda-edge-policy-{self.idx}"),
            role=lambda_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
        )

        # Create Lambda@Edge function
        lambda_edge_code = set_custom_host_header(parsed.netloc)
        lambda_edge = pulumi_aws.lambda_.Function(
            context().prefix(f"url-origin-host-rewrite-{self.idx}"),
            runtime="nodejs20.x",
            role=lambda_role.arn,
            handler="index.handler",
            code=pulumi.AssetArchive({"index.js": pulumi.StringAsset(lambda_edge_code)}),
            publish=True,  # Required for Lambda@Edge
            opts=pulumi.ResourceOptions(
                # Lambda@Edge must be in us-east-1
                provider=pulumi_aws.Provider(
                    context().prefix(f"url-origin-us-east-1-provider-{self.idx}"),
                    region="us-east-1",
                )
            ),
        )

        cache_behavior = {
            "path_pattern": path_pattern,
            "allowed_methods": [
                "GET",
                "HEAD",
                "OPTIONS",
                "PUT",
                "POST",
                "PATCH",
                "DELETE",
            ],
            "cached_methods": ["GET", "HEAD"],
            "target_origin_id": origin_dict["origin_id"],
            "compress": True,
            "viewer_protocol_policy": "redirect-to-https",
            "forwarded_values": {
                # Forward everything so the origin sees original request
                "query_string": True,
                "cookies": {"forward": "all"},
                # Forward all headers except Host (which Lambda@Edge will set)
                "headers": ["*"],
            },
            "min_ttl": 0,
            "default_ttl": 0,
            "max_ttl": 0,
            "function_associations": [
                {
                    "event_type": "viewer-request",
                    "function_arn": cf_function.arn,
                }
            ],
            "lambda_function_associations": [
                {
                    "event_type": "origin-request",
                    "lambda_arn": lambda_edge.qualified_arn,
                    "include_body": True,
                }
            ],
        }

        return RouteOriginConfig(
            origin_access_controls=None,
            origins=origin_dict,
            ordered_cache_behaviors=cache_behavior,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(
        self,
        distribution: pulumi_aws.cloudfront.Distribution,  # noqa: ARG002
    ) -> pulumi_aws.s3.BucketPolicy | None:
        return None
