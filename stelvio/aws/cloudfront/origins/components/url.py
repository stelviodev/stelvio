import hashlib
from dataclasses import dataclass
from typing import Any, final
from urllib.parse import urlparse

import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import RouteOriginConfig
from stelvio.aws.cloudfront.js import set_custom_host_header
from stelvio.aws.cloudfront.origins.base import (
    API_ALLOWED_METHODS,
    API_CACHED_METHODS,
    ComponentCloudfrontAdapter,
)
from stelvio.aws.cloudfront.origins.registry import register_adapter
from stelvio.component import Component
from stelvio.context import context
from stelvio.link import LinkableMixin
from stelvio.provider import ProviderStore


@final
@dataclass(frozen=True)
class UrlResources:
    url: str


@final
class Url(Component[UrlResources, Any], LinkableMixin):
    def __init__(self, name: str, url: str, *, tags: dict[str, str] | None = None):
        super().__init__(ProviderStore.aws(), "stelvio:aws:Url", name, tags=tags)
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
        self.register_outputs({"url": self.url})

        return UrlResources(
            url=self.url,
        )


def url_origin_name(router_name: str, url: str) -> str:
    """Stem of a url route's resources, and the name `Router` gives the `Url` it wraps a
    url string in. The hashed url, not the `Url`'s name, so a hand-made `Url` gets the
    same shape."""
    sha_url = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{router_name}-url-origin-{sha_url}"


@register_adapter(Url)
class UrlCloudfrontAdapter(ComponentCloudfrontAdapter):
    @property
    def url(self) -> Url:
        return self.route.component

    @property
    def _stem(self) -> str:
        return url_origin_name(self.router_name, self.url.resources.url)

    @property
    def _old_stem(self) -> str:
        return "url-origin"

    def get_origin_config(self) -> RouteOriginConfig:
        parsed = urlparse(self.url.resources.url)
        tags = self.url.tags or None

        origin_id = context().prefix(self._stem)

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

        cf_function = self._uri_rewrite_function(depends_on=[])

        # Lambda@Edge for Host header rewriting (origin-request)
        # Create IAM role for Lambda@Edge
        lambda_role = pulumi_aws.iam.Role(
            self._name("lambda-edge-role", limit=64),
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
            tags=tags,
            opts=self._opts("lambda-edge-role"),
        )

        # Attach basic execution policy
        pulumi_aws.iam.RolePolicyAttachment(
            self._name("lambda-edge-policy", limit=None),
            role=lambda_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            opts=self._opts("lambda-edge-policy"),
        )

        # Create Lambda@Edge function
        lambda_edge_code = set_custom_host_header(parsed.netloc)
        lambda_edge = pulumi_aws.lambda_.Function(
            self._name("host-rewrite", limit=64),
            runtime="nodejs22.x",
            role=lambda_role.arn,
            handler="index.handler",
            code=pulumi.AssetArchive({"index.js": pulumi.StringAsset(lambda_edge_code)}),
            publish=True,  # Required for Lambda@Edge
            tags=tags,
            # Lambda@Edge must be in us-east-1
            opts=self._opts("host-rewrite", provider=ProviderStore.aws_for_region("us-east-1")),
        )

        cache_behaviors = self._cache_behaviors(
            {
                "allowed_methods": API_ALLOWED_METHODS,
                "cached_methods": API_CACHED_METHODS,
                "target_origin_id": origin_dict["origin_id"],
                "compress": True,
                "viewer_protocol_policy": "redirect-to-https",
                "forwarded_values": {
                    "query_string": True,
                    "cookies": {"forward": "all"},
                    # Host too; the Lambda@Edge function below overwrites it
                    "headers": ["*"],
                },
                "min_ttl": 0,
                "default_ttl": 0,
                "max_ttl": 0,
                "function_associations": self._function_associations(cf_function),
                "lambda_function_associations": [
                    {
                        "event_type": "origin-request",
                        "lambda_arn": lambda_edge.qualified_arn,
                        "include_body": True,
                    }
                ],
            }
        )

        return RouteOriginConfig(
            origin_access_controls=None,
            origins=origin_dict,
            ordered_cache_behaviors=cache_behaviors,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(
        self,
        distribution: pulumi_aws.cloudfront.Distribution,  # noqa: ARG002
    ) -> pulumi_aws.s3.BucketPolicy | None:
        return None
