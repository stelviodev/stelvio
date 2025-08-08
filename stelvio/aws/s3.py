from dataclasses import dataclass
from typing import final

import pulumi
import pulumi_aws
from pulumi import Output

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig


@dataclass(frozen=True)
class S3BucketResources:
    bucket: pulumi_aws.s3.Bucket
    cloudfront_distribution: CloudFrontDistribution | None = None


@final
class Bucket(Component[S3BucketResources], Linkable):
    def __init__(
        self,
        name: str,
        storage_class: str = "STANDARD",
        versioning_enabled: bool = False,
        public_access_block: bool = True,
        enforce_https: bool = True,
        custom_domain: str | None = None,
    ):
        super().__init__(name)
        self.storage_class = storage_class
        self.versioning_enabled = versioning_enabled
        self.public_access_block = public_access_block
        self.enforce_https = enforce_https
        self.custom_domain = custom_domain
        self._resources = None

    @property
    def arn(self) -> Output[str]:
        return self.resources.bucket.arn

    @property
    def cloudfront_domain_name(self) -> Output[str] | None:
        """Returns the CloudFront distribution domain name if custom domain is configured."""
        if self.resources.cloudfront_distribution:
            return self.resources.cloudfront_distribution.domain_name
        return None

    @property
    def website_url(self) -> Output[str]:
        """Returns the website URL (custom domain if configured, otherwise S3 website endpoint)."""
        if self.custom_domain:
            return pulumi.Output.concat("https://", self.custom_domain)
        return self.resources.bucket.website_endpoint

    def _create_resources(self) -> S3BucketResources:
        bucket = pulumi_aws.s3.Bucket(
            context().prefix(self.name),
            versioning={"enabled": self.versioning_enabled},
            website={
                "indexDocument": "index.html",
                "errorDocument": "error.html",
            }
        )

        # Configure public access block
        public_access_block = pulumi_aws.s3.BucketPublicAccessBlock(
            context().prefix(f"{self.name}-pab"),
            bucket=bucket.id,
            block_public_acls=self.public_access_block,
            block_public_policy=self.public_access_block,
            ignore_public_acls=self.public_access_block,
            restrict_public_buckets=self.public_access_block,
        )

        cloudfront_distribution = None
        
        if self.custom_domain:
            # Create ACM certificate for the custom domain
            acm_validated_domain = AcmValidatedDomain(
                name=self.custom_domain,
                domain_name=self.custom_domain,
            )

            # Create CloudFront distribution
            cloudfront_distribution = CloudFrontDistribution(
                name=f"{self.name}-cloudfront",
                s3_bucket=bucket,
                custom_domain=self.custom_domain,
                certificate_arn=acm_validated_domain.resources.certificate.arn,
            )

            # Create DNS record pointing to CloudFront distribution
            record = context().dns.create_record(
                resource_name=f"s3bucket_{self.name}-custom-domain-record",
                name=self.custom_domain,
                record_type="CNAME",
                value=cloudfront_distribution.domain_name,
            )
            pulumi.export(f"s3bucket_{self.name}_custom_domain_record", record.name)
            pulumi.export(f"s3bucket_{self.name}_cloudfront_domain", cloudfront_distribution.domain_name)

        pulumi.export(f"s3bucket_{self.name}_arn", bucket.arn)
        pulumi.export(f"s3bucket_{self.name}_name", bucket.bucket)
        pulumi.export(f"s3bucket_{self.name}_website_endpoint", bucket.website_endpoint)
        
        return S3BucketResources(bucket, cloudfront_distribution)

    def link(self) -> Link:
        link_creator_ = ComponentRegistry.get_link_config_creator(type(self))

        link_config = link_creator_(self.resources.bucket)
        return Link(self.name, link_config.properties, link_config.permissions)


@link_config_creator(Bucket)
def default_bucket_link(bucket: pulumi_aws.s3.Bucket) -> LinkConfig:
    return LinkConfig(
        properties={"bucket_arn": bucket.arn, "bucket_name": bucket.bucket},
        permissions=[
            AwsPermission(
                actions=["s3:ListBucket"],
                resources=[bucket.arn],
            ),
            AwsPermission(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[bucket.arn.apply(lambda arn: f"{arn}/*")],
            ),
        ],
    )
