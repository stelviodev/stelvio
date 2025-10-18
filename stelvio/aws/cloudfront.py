from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, TypedDict, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway.constants import HTTPMethodInput
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


@dataclass(frozen=True)
class CloudFrontDistributionResources:
    distribution: pulumi_aws.cloudfront.Distribution
    origin_access_control: pulumi_aws.cloudfront.OriginAccessControl
    acm_validated_domain: AcmValidatedDomain
    record: Record
    bucket_policy: pulumi_aws.s3.BucketPolicy
    function_associations: list[FunctionAssociation] | None


@final
class CloudFrontDistribution(Component[CloudFrontDistributionResources]):
    def __init__(
        self,
        name: str,
        bucket: Bucket,
        price_class: CloudfrontPriceClass = "PriceClass_100",
        custom_domain: str | None = None,
        function_associations: list[FunctionAssociation] | None = None,
    ):
        super().__init__(name)
        self.bucket = bucket
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
            aliases=[self.custom_domain] if self.custom_domain else None,
            origins=[
                {
                    "domain_name": self.bucket.resources.bucket.bucket_regional_domain_name,
                    "origin_id": f"{self.name}-S3-Origin",
                    "origin_access_control_id": origin_access_control.id,
                }
            ],
            enabled=True,
            is_ipv6_enabled=True,
            default_root_object="index.html",
            default_cache_behavior={
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
            },
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
            custom_error_responses=[
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
            ],
        )

        # Update S3 bucket policy to allow CloudFront access
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

        pulumi.export(f"cloudfront_{self.name}_bucket_policy", bucket_policy.id)

        return CloudFrontDistributionResources(
            distribution,
            origin_access_control,
            acm_validated_domain,
            record,
            bucket_policy,
            self.function_associations,
        )


@final
class CloudfrontRoute:
    def __init__(self, path_pattern: str, component: Component):
        self.path_pattern = path_pattern
        self.component = component


@dataclass(frozen=True)
class CloudfrontRouterResources:
    distribution: pulumi_aws.cloudfront.Distribution
    origin_access_controls: list[pulumi_aws.cloudfront.OriginAccessControl]
    bucket_policies: list[pulumi_aws.s3.BucketPolicy]
    acm_validated_domain: AcmValidatedDomain | None
    record: Record | None


@final
class CloudfrontRouter(Component[CloudfrontRouterResources]):
    def __init__(
        self,
        name: str,
        routes: list[CloudfrontRoute] | None = None,
        price_class: CloudfrontPriceClass = "PriceClass_100",
        custom_domain: str | None = None,
    ):
        super().__init__(name)
        self.routes = routes or []
        self.price_class = price_class
        self.custom_domain = custom_domain

    def _create_resources(self) -> CloudfrontRouterResources:
        # Create ACM Validated Domain if custom domain is provided
        acm_validated_domain = None
        if self.custom_domain:
            if context().dns is None:
                raise DnsProviderNotConfiguredError("DNS not configured.")
            acm_validated_domain = AcmValidatedDomain(
                f"{self.name}-acm-validated-domain",
                domain_name=self.custom_domain,
            )
        
        # Create Origin Access Controls for S3 buckets
        origin_access_controls = []
        origins = []
        
        for idx, route in enumerate(self.routes):
            # Create OAC for each S3 origin
            oac = pulumi_aws.cloudfront.OriginAccessControl(
                context().prefix(f"{self.name}-oac-{idx}"),
                description=f"Origin Access Control for {self.name} route {idx}",
                origin_access_control_origin_type="s3",
                signing_behavior="always",
                signing_protocol="sigv4",
            )
            origin_access_controls.append(oac)
            
            # Get origin configuration from component
            origin_args = route.component.cloudfront_origin(self.custom_domain)
            
            # Create a proper origin dict with OAC
            origin_dict = {
                "origin_id": origin_args.origin_id,
                "domain_name": origin_args.domain_name,
                "origin_access_control_id": oac.id,
            }
            origins.append(origin_dict)

        distribution = pulumi_aws.cloudfront.Distribution(
            context().prefix(self.name),
            aliases=[self.custom_domain] if self.custom_domain else None,
            origins=origins,
            enabled=True,
            is_ipv6_enabled=True,
            default_root_object="index.html",
            default_cache_behavior={
                "allowed_methods": ["GET", "HEAD", "OPTIONS"],
                "cached_methods": ["GET", "HEAD"],
                "target_origin_id": origins[0]["origin_id"] if origins else "default-origin",
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
            },
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
        )
        
        # Create bucket policies to allow CloudFront access for each S3 bucket
        bucket_policies = []
        for idx, route in enumerate(self.routes):
            # Get the bucket from the component (assuming it's a Bucket component)
            if hasattr(route.component, 'resources') and hasattr(route.component.resources, 'bucket'):
                bucket = route.component.resources.bucket
                bucket_arn = route.component.arn
                
                import json
                
                bucket_policy = pulumi_aws.s3.BucketPolicy(
                    context().prefix(f"{self.name}-bucket-policy-{idx}"),
                    bucket=bucket.id,
                    policy=pulumi.Output.all(
                        distribution_arn=distribution.arn,
                        bucket_arn=bucket_arn,
                    ).apply(
                        lambda args: json.dumps({
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
                        })
                    ),
                    opts=pulumi.ResourceOptions(
                        depends_on=[distribution]
                    ),
                )
                bucket_policies.append(bucket_policy)

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
        pulumi.export("num_origins", len(origins))

        return CloudfrontRouterResources(
            distribution=distribution,
            origin_access_controls=origin_access_controls,
            bucket_policies=bucket_policies,
            acm_validated_domain=acm_validated_domain,
            record=record,
        )

    def _add_route(self, route: CloudfrontRoute) -> None:
        self.routes.append(route)

    def _remove_route(self, route: CloudfrontRoute) -> None:
        self.routes.remove(route)

    def route(
        self,
        http_method: HTTPMethodInput,
        path: str,
        component: Component,
    ):
        self._add_route(CloudfrontRoute(path, component))
