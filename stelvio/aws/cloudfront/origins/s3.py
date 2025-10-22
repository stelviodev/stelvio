from dataclasses import dataclass

import pulumi_aws

from stelvio.aws.s3.s3 import Bucket
from stelvio.component import Component
from stelvio.context import context


@dataclass(frozen=True)
class CloudflareRouterRouteOriginConfig:
    origin_access_controls: pulumi_aws.cloudfront.OriginAccessControl
    origins : dict
    ordered_cache_behaviors : dict
    cloudfront_functions : pulumi_aws.cloudfront.Function
    
class S3BucketCloudfrontBridge:
    def __init__(self, bucket: Bucket, idx: int, route: any) -> None:
        self.bucket = bucket
        self.idx = idx
        self.route = route

    @staticmethod
    def match(stlv_component: Component) -> bool:
        return isinstance(stlv_component, Bucket)

    def get_origin_config(self):
        oac = pulumi_aws.cloudfront.OriginAccessControl(
                context().prefix(f"{self.bucket.name}-oac-{self.idx}"),
                description=f"Origin Access Control for {self.bucket.name} route {self.idx}",
                origin_access_control_origin_type="s3",
                signing_behavior="always",
                signing_protocol="sigv4",
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
        path_pattern = f"{self.route.path_pattern}/*" if not self.route.path_pattern.endswith("*") else self.route.path_pattern
        function_code = strip_path_pattern_function_js(self.route.path_pattern)
        cf_function = pulumi_aws.cloudfront.Function(
                context().prefix(f"{self.bucket.name}-uri-rewrite-{self.idx}"),
                runtime="cloudfront-js-2.0",
                code=function_code,
                comment=f"Strip {self.route.path_pattern} prefix for route {self.idx}",
            )
        cache_behavior = {
                "path_pattern": path_pattern,
                "allowed_methods": ["GET", "HEAD", "OPTIONS"],
                "cached_methods": ["GET", "HEAD"],
                # "target_origin_id": origins[idx]["origin_id"],
                "target_origin_id": origin_dict["origin_id"],
                "compress": True,
                "viewer_protocol_policy": "redirect-to-https",
                "forwarded_values": {
                    "query_string": False,
                    "cookies": {"forward": "none"},
                    "headers": ["If-Modified-Since"],
                },
                "min_ttl": 0,
                "default_ttl": 300,
                "max_ttl": 3600,
                "function_associations": [
                    {
                        "event_type": "viewer-request",
                        "function_arn": cf_function.arn,
                    }
                ],
            }
        route_config = CloudflareRouterRouteOriginConfig(
                origin_access_controls=oac,
                origins=origin_dict,
                ordered_cache_behaviors=cache_behavior,
                cloudfront_functions=cf_function,
            )
        return route_config