import json

import pulumi
import pulumi_aws

from stelvio.aws.cloudfront.dtos import Route, RouteOriginConfig
from stelvio.aws.cloudfront.js import strip_path_pattern_function_js
from stelvio.aws.cloudfront.origins.base import ComponentCloudfrontAdapter
from stelvio.aws.cloudfront.origins.decorators import register_adapter
from stelvio.aws.s3.s3_static_website import REQUEST_INDEX_HTML_FUNCTION_JS, S3StaticWebsite
from stelvio.context import context


@register_adapter(S3StaticWebsite)
class S3BucketCloudfrontAdapter(ComponentCloudfrontAdapter):
    def __init__(self, idx: int, route: Route) -> None:
        super().__init__(idx, route)
        self.bucket = None
        self.function_resource = None
        if route.component.resources.bucket:
            self.bucket = route.component
        if route.component.resources._function_resource: # noqa: SLF001
            self.function_resource = route.component.resources._function_resource # noqa: SLF001
            self.function_url_resource = route.component.resources._function_resource_url # noqa: SLF001

    def get_origin_config(self) -> RouteOriginConfig:
        if self.bucket:
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
            # function_code = strip_path_pattern_function_js(self.route.path_pattern)
            # cf_function = pulumi_aws.cloudfront.Function(
            #     context().prefix(f"{self.bucket.name}-uri-rewrite-{self.idx}"),
            #     runtime="cloudfront-js-2.0",
            #     code=function_code,
            #     comment=f"Strip {self.route.path_pattern} prefix for route {self.idx}",
            #     opts=pulumi.ResourceOptions(depends_on=[self.bucket.resources.bucket]),
            # )
            cf_function = pulumi_aws.cloudfront.Function(
                context().prefix(f"{self.bucket.name}-viewer-request-function-router-{self.idx}"),
                name=context().prefix(
                    f"{self.bucket.name}-viewer-request-function-router-{self.idx}"
                ),
                runtime="cloudfront-js-1.0",
                comment="Rewrite requests to directories to serve index.html",
                code=REQUEST_INDEX_HTML_FUNCTION_JS,  # TODO: (configurable?)
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
                "default_ttl": 1,  # 86400,  # 1 day
                "max_ttl": 1,  # 31536000,  # 1 year
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

        if self.function_resource:
            # function_url = pulumi_aws.lambda_.FunctionUrl(
            #     safe_name(context().prefix(), f"{self.function_resource.name}-stub-url", 64),
            #     function_name=self.function_resource.name,
            #     authorization_type="AWS_IAM",
            # )


            function_url = self.function_url_resource

            # Extract domain from function URL (remove https:// and trailing /)
            function_domain = function_url.function_url.apply(
                lambda url: url.replace("https://", "").rstrip("/")
            )

            origin_args = pulumi_aws.cloudfront.DistributionOriginArgs(
                origin_id=self.function_resource.name,
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

            # # Add OAC if using IAM auth
            # if oac is not None:
            #     origin_dict["origin_access_control_id"] = oac.id

            function_code = strip_path_pattern_function_js(self.route.path_pattern)
            cf_function = pulumi_aws.cloudfront.Function(
                # context().prefix(f"{self.function_resource.name}-uri-rewrite-{self.idx}"),
                "test-name-function",  # TODO
                runtime="cloudfront-js-2.0",
                code=function_code,
                comment=f"Strip {self.route.path_pattern} prefix for route {self.idx}",
                opts=pulumi.ResourceOptions(depends_on=[self.function_resource]),
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

            oac = pulumi_aws.cloudfront.OriginAccessControl(
                # context().prefix(f"{self.function.name}-oac-{self.idx}"),
                "test-name-oac",  # TODO
                description="OAC for Lambda Function ",
                origin_access_control_origin_type="lambda",
                signing_behavior="always",
                signing_protocol="sigv4",
                opts=pulumi.ResourceOptions(depends_on=[self.function_resource]),
            )
            # Add OAC if using IAM auth
            if oac is not None:
                origin_dict["origin_access_control_id"] = oac.id

            return RouteOriginConfig(
                origin_access_controls=oac,
                origins=origin_dict,
                ordered_cache_behaviors=ordered_cache_behaviors,
                cloudfront_functions=cf_function,
            )
        return None

    def get_access_policy(
        self, distribution: pulumi_aws.cloudfront.Distribution
    ) -> pulumi_aws.s3.BucketPolicy:
        if self.bucket:
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
        if self.function_resource:
            # Grant cloudfront.amazonaws.com permission to invoke via Function URL
            return pulumi_aws.lambda_.Permission(
                # context().prefix(f"{self.function.name}-cloudfront-permission-{self.idx}"),
                "test-name-permission",  # TODO
                action="lambda:InvokeFunctionUrl",
                function=self.function_resource.name,
                principal="cloudfront.amazonaws.com",
                source_arn=distribution.arn,
                function_url_auth_type="AWS_IAM",
            )
        return None
