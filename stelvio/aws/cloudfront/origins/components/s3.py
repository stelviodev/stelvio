import json

import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter
from stelvio.aws.cloudfront.origins.decorators import register_adapter
from stelvio.aws.s3.s3 import Bucket
from stelvio.context import context


@register_adapter(Bucket)
class S3BucketCloudfrontAdapter(ComponentCloudfrontAdapter):
    def __init__(self, idx: int, route: Route) -> None:
        super().__init__(idx, route)
        self.bucket = route.component

    def get_origin_config(self) -> RouteOriginConfig:
        oac = pulumi_aws.cloudfront.OriginAccessControl(
            context().prefix(f"{self.bucket.name}-oac-{self.idx}"),
            description=f"Origin Access Control for {self.bucket.name} route {self.idx}",
            origin_access_control_origin_type="s3",
            signing_behavior="always",
            signing_protocol="sigv4",
            opts=pulumi.ResourceOptions(depends_on=[self.bucket.resources.bucket]),
        )
        origin_args = pulumi_aws.cloudfront.DistributionOriginArgs(
            origin_id=self.bucket.resources.bucket.arn,
            domain_name=self.bucket.resources.bucket.bucket_regional_domain_name,
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
            context().prefix(f"{self.bucket.name}-uri-rewrite-{self.idx}"),
            runtime="cloudfront-js-2.0",
            code=function_code,
            comment=f"Strip {self.route.path_pattern} prefix for route {self.idx}",
            opts=pulumi.ResourceOptions(depends_on=[self.bucket.resources.bucket]),
        )
        cache_behavior = {
            "path_pattern": path_pattern,
            "allowed_methods": ["GET", "HEAD", "OPTIONS"],
            "cached_methods": ["GET", "HEAD"],
            "target_origin_id": origin_dict["origin_id"],
            "compress": True,
            "viewer_protocol_policy": "redirect-to-https",
            "forwarded_values": {
                "query_string": False,
                "cookies": {"forward": "none"},
                "headers": ["If-Modified-Since"],
            },
            "min_ttl": 0,
            "default_ttl": 86400,  # 1 day
            "max_ttl": 31536000,  # 1 year
            "function_associations": [
                {
                    "event_type": "viewer-request",
                    "function_arn": cf_function.arn,
                }
            ],
        }
        return RouteOriginConfig(
            origin_access_controls=oac,
            origins=origin_dict,
            ordered_cache_behaviors=cache_behavior,
            cloudfront_functions=cf_function,
        )

    def get_access_policy(
        self, distribution: pulumi_aws.cloudfront.Distribution
    ) -> pulumi_aws.s3.BucketPolicy:
        bucket = self.bucket.resources.bucket
        bucket_arn = bucket.arn

        return pulumi_aws.s3.BucketPolicy(
            context().prefix(f"{self.bucket.name}-bucket-policy-{self.idx}"),
            bucket=bucket.id,
            policy=pulumi.Output.all(
                distribution_arn=distribution.arn,
                bucket_arn=bucket_arn,
            ).apply(
                lambda args: json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Sid": "AllowCloudFrontServicePrincipal",
                                "Effect": "Allow",
                                "Principal": {"Service": "cloudfront.amazonaws.com"},
                                "Action": "s3:GetObject",
                                "Resource": f"{args['bucket_arn']}/*",
                                "Condition": {
                                    "StringEquals": {"AWS:SourceArn": args["distribution_arn"]}
                                },
                            }
                        ],
                    }
                )
            ),
        )
