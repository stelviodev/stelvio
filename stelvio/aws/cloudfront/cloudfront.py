from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.component import Component
from stelvio.dns import DnsProviderNotConfiguredError

if TYPE_CHECKING:
    from stelvio.aws.s3.s3 import Bucket
    from stelvio.dns import Record


# TODO: Consider using internal names for these
# https://www.pulumi.com/registry/packages/aws/api-docs/cloudfront/distribution/#inputs
CloudfrontPriceClass = Literal["PriceClass_100", "PriceClass_200", "PriceClass_All"]


class FunctionAssociation(TypedDict):
    event_type: str
    function_arn: str


@final
@dataclass(frozen=True)
class CloudFrontDistributionResources:
    distribution: pulumi_aws.cloudfront.Distribution
    origin_access_control: pulumi_aws.cloudfront.OriginAccessControl
    acm_validated_domain: AcmValidatedDomain | None
    record: Record | None
    bucket_policy: pulumi_aws.s3.BucketPolicy | None
    function_associations: list[FunctionAssociation] | None


@final
class CloudFrontDistribution(Component[CloudFrontDistributionResources]):
    def __init__(
        self,
        name: str,
        bucket: Bucket,
        _function_resource: pulumi.Resource | None = None,
        price_class: CloudfrontPriceClass = "PriceClass_100",
        custom_domain: str | None = None,
        function_associations: list[FunctionAssociation] | None = None,
    ):
        super().__init__(name)
        self.bucket = bucket
        self._function_resource = _function_resource
        self.custom_domain = custom_domain
        self.price_class = price_class
        self.function_associations = function_associations or []
        self._resources = None

    def _create_resources(self) -> CloudFrontDistributionResources:
        # Create ACM Validated Domain if custom domain is provided
        acm_validated_domain = None
        if self.custom_domain:
            if context().dns is None:
                raise DnsProviderNotConfiguredError("DNS not configured.")
            acm_validated_domain = AcmValidatedDomain(
                f"{self.name}-acm-validated-domain",
                domain_name=self.custom_domain,
            )

        if self._function_resource:
            # Create Origin Access Control for Lambda
            origin_access_control = pulumi_aws.cloudfront.OriginAccessControl(
                context().prefix(f"{self.name}-oac-lambda"),
                description=f"Origin Access Control for {self.name}",
                origin_access_control_origin_type="lambda",
                signing_behavior="always",
                signing_protocol="sigv4",
            )

            origins = [
                {
                    "domain_name": self._function_resource.function_url.apply(
                        lambda url: url.replace("https://", "").rstrip("/")
                    ),
                    "origin_id": f"{self.name}-Lambda-Origin",
                    "origin_access_control_id": origin_access_control.id,
                    "custom_origin_config": {
                        "http_port": 80,
                        "https_port": 443,
                        "origin_protocol_policy": "https-only",
                        "origin_ssl_protocols": ["TLSv1.2"],
                    },
                }
            ]

            default_cache_behavior = {
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
                "target_origin_id": f"{self.name}-Lambda-Origin",
                "compress": True,
                "viewer_protocol_policy": "redirect-to-https",
                # AllViewerExceptHostHeader:
                "origin_request_policy_id": "b689b0a8-53d0-40ab-baf2-68738e2966ac",
                # CachingDisabled:
                "cache_policy_id": "4135ea2d-6df8-44a3-9df3-4b5a84be39ad",
                "function_associations": self.function_associations,
            }

            default_root_object = None
            custom_error_responses = []

        else:
            # Create Origin Access Control for S3
            origin_access_control = pulumi_aws.cloudfront.OriginAccessControl(
                context().prefix(f"{self.name}-oac-s3"),
                description=f"Origin Access Control for {self.name}",
                origin_access_control_origin_type="s3",
                signing_behavior="always",
                signing_protocol="sigv4",
            )

            origins = [
                {
                    "domain_name": self.bucket.resources.bucket.bucket_regional_domain_name,
                    "origin_id": f"{self.name}-S3-Origin",
                    "origin_access_control_id": origin_access_control.id,
                }
            ]

            default_cache_behavior = {
                "allowed_methods": ["GET", "HEAD", "OPTIONS"],  # Reduced to read-only methods
                "cached_methods": ["GET", "HEAD"],
                "target_origin_id": f"{self.name}-S3-Origin",
                "compress": True,
                "viewer_protocol_policy": "redirect-to-https",
                "forwarded_values": {
                    "query_string": False,
                    "cookies": {"forward": "none"},
                    "headers": ["If-Modified-Since"],  # Forward cache validation headers
                },
                "min_ttl": 0,
                "default_ttl": 300,  # Reduce default TTL to 5 minutes for faster updates
                "max_ttl": 3600,  # Reduce max TTL to 1 hour
                "function_associations": self.function_associations,
            }

            default_root_object = "index.html"
            custom_error_responses = [
                {
                    "error_code": 403,
                    "response_code": 404,
                    "response_page_path": "/error.html",
                    "error_caching_min_ttl": 0,  # Don't cache 403 errors
                },
                {
                    "error_code": 404,
                    "response_code": 404,
                    "response_page_path": "/error.html",
                    "error_caching_min_ttl": 300,  # Cache 404s for only 5 minutes
                },
            ]

        # Create CloudFront Distribution
        distribution = pulumi_aws.cloudfront.Distribution(
            context().prefix(self.name),
            aliases=[self.custom_domain] if self.custom_domain else None,
            origins=origins,
            enabled=True,
            is_ipv6_enabled=True,
            default_root_object=default_root_object,
            default_cache_behavior=default_cache_behavior,
            price_class=self.price_class,
            restrictions={
                "geo_restriction": {
                    "restriction_type": "none",
                }
            },
            viewer_certificate={
                "acm_certificate_arn": acm_validated_domain.resources.certificate.arn,
                "ssl_support_method": "sni-only",
                "minimum_protocol_version": "TLSv1.2_2021",
            }
            if self.custom_domain
            else {
                "cloudfront_default_certificate": True,
            },
            custom_error_responses=custom_error_responses,
        )

        # Update S3 bucket policy to allow CloudFront access
        bucket_policy = None
        if not self._function_resource:
            bucket_policy = pulumi_aws.s3.BucketPolicy(
                context().prefix(f"{self.name}-bucket-policy"),
                bucket=self.bucket.resources.bucket.id,
                policy=pulumi.Output.all(
                    distribution_arn=distribution.arn,
                    bucket_arn=self.bucket.arn,
                ).apply(
                    lambda args: pulumi.Output.json_dumps(
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
                opts=pulumi.ResourceOptions(
                    depends_on=[distribution]
                ),  # Ensure policy is applied after distribution
            )
        else:
            # Allow CloudFront to invoke the Lambda function URL
            pulumi_aws.lambda_.Permission(
                context().prefix(f"{self.name}-cloudfront-invoke"),
                action="lambda:InvokeFunctionUrl",
                function=self._function_resource.name,
                principal="cloudfront.amazonaws.com",
                source_arn=distribution.arn,
                opts=pulumi.ResourceOptions(depends_on=[distribution]),
            )

        record = None
        if self.custom_domain:
            record = context().dns.create_record(
                resource_name=context().prefix(f"{self.name}-cloudfront-record"),
                name=self.custom_domain,
                record_type="CNAME",
                value=distribution.domain_name,
                ttl=1,
            )

        pulumi.export(f"cloudfront_{self.name}_domain_name", distribution.domain_name)
        pulumi.export(f"cloudfront_{self.name}_distribution_id", distribution.id)
        pulumi.export(f"cloudfront_{self.name}_arn", distribution.arn)

        if record:
            pulumi.export(f"cloudfront_{self.name}_record_name", record.pulumi_resource.name)

        with contextlib.suppress(Exception):
            pulumi.export(f"cloudfront_{self.name}_bucket_policy", bucket_policy.id)

        return CloudFrontDistributionResources(
            distribution,
            origin_access_control,
            acm_validated_domain,
            record,
            bucket_policy,
            self.function_associations,
        )
