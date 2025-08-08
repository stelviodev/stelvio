from dataclasses import dataclass
from typing import final

import pulumi
import pulumi_aws
from pulumi import Output

from stelvio import context
from stelvio.component import Component


@dataclass(frozen=True)
class CloudFrontDistributionResources:
    distribution: pulumi_aws.cloudfront.Distribution
    origin_access_control: pulumi_aws.cloudfront.OriginAccessControl
    viewer_request_function: pulumi_aws.cloudfront.Function


@final
class CloudFrontDistribution(Component[CloudFrontDistributionResources]):
    def __init__(
        self,
        name: str,
        s3_bucket: pulumi_aws.s3.Bucket,
        custom_domain: str,
        certificate_arn: Output[str],
        price_class: str = "PriceClass_100",
    ):
        super().__init__(name)
        self.s3_bucket = s3_bucket
        self.custom_domain = custom_domain
        self.certificate_arn = certificate_arn
        self.price_class = price_class
        self._resources = None

    @property
    def domain_name(self) -> Output[str]:
        return self.resources.distribution.domain_name

    @property
    def arn(self) -> Output[str]:
        return self.resources.distribution.arn

    def _create_resources(self) -> CloudFrontDistributionResources:
        # Create CloudFront Function to handle directory index rewriting
        viewer_request_function = pulumi_aws.cloudfront.Function(
            context().prefix(f"{self.name}-viewer-request"),
            name=context().prefix(f"{self.name}-viewer-request"),
            runtime="cloudfront-js-1.0",
            comment="Rewrite requests to directories to serve index.html",
            code="""
function handler(event) {
    var request = event.request;
    var uri = request.uri;
    
    // Check whether the URI is missing a file name.
    if (uri.endsWith('/')) {
        request.uri += 'index.html';
    }
    // Check whether the URI is missing a file extension.
    else if (!uri.includes('.')) {
        request.uri += '/index.html';
    }
    
    return request;
}
            """.strip(),
        )

        # Create Origin Access Control for S3
        origin_access_control = pulumi_aws.cloudfront.OriginAccessControl(
            context().prefix(f"{self.name}-oac"),
            description=f"Origin Access Control for {self.name}",
            origin_access_control_origin_type="s3",
            signing_behavior="always",
            signing_protocol="sigv4",
        )

        # Create CloudFront Distribution
        distribution = pulumi_aws.cloudfront.Distribution(
            context().prefix(self.name),
            aliases=[self.custom_domain],
            origins=[
                {
                    "domain_name": self.s3_bucket.bucket_regional_domain_name,
                    "origin_id": f"{self.name}-S3-Origin",
                    "origin_access_control_id": origin_access_control.id,
                }
            ],
            enabled=True,
            is_ipv6_enabled=True,
            default_root_object="index.html",
            default_cache_behavior={
                "allowed_methods": ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"],
                "cached_methods": ["GET", "HEAD"],
                "target_origin_id": f"{self.name}-S3-Origin",
                "compress": True,
                "viewer_protocol_policy": "redirect-to-https",
                "forwarded_values": {
                    "query_string": False,
                    "cookies": {"forward": "none"},
                    "headers": ["If-Match", "If-None-Match"],  # Forward ETag headers for proper caching
                },
                "min_ttl": 0,
                "default_ttl": 3600,
                "max_ttl": 86400,
                "function_associations": [
                    {
                        "event_type": "viewer-request",
                        "function_arn": viewer_request_function.arn,
                    }
                ],
            },
            price_class=self.price_class,
            restrictions={
                "geo_restriction": {
                    "restriction_type": "none",
                }
            },
            viewer_certificate={
                "acm_certificate_arn": self.certificate_arn,
                "ssl_support_method": "sni-only",
                "minimum_protocol_version": "TLSv1.2_2021",
            },
            custom_error_responses=[
                {
                    "error_code": 403,
                    "response_code": 404,
                    "response_page_path": "/error.html",
                },
                {
                    "error_code": 404,
                    "response_code": 404,
                    "response_page_path": "/error.html",
                },
            ],
        )

        # Update S3 bucket policy to allow CloudFront access
        bucket_policy = pulumi_aws.s3.BucketPolicy(
            context().prefix(f"{self.name}-bucket-policy"),
            bucket=self.s3_bucket.id,
            policy=pulumi.Output.all(
                distribution_arn=distribution.arn,
                bucket_arn=self.s3_bucket.arn,
            ).apply(
                lambda args: {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AllowCloudFrontServicePrincipal",
                            "Effect": "Allow",
                            "Principal": {
                                "Service": "cloudfront.amazonaws.com"
                            },
                            "Action": "s3:GetObject",
                            "Resource": f"{args['bucket_arn']}/*",
                            "Condition": {
                                "StringEquals": {
                                    "AWS:SourceArn": args['distribution_arn']
                                }
                            }
                        }
                    ]
                }
            ),
        )

        pulumi.export(f"cloudfront_{self.name}_domain_name", distribution.domain_name)
        pulumi.export(f"cloudfront_{self.name}_distribution_id", distribution.id)
        pulumi.export(f"cloudfront_{self.name}_arn", distribution.arn)

        return CloudFrontDistributionResources(distribution, origin_access_control, viewer_request_function)
