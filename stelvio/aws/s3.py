import mimetypes
import os
import re
from dataclasses import dataclass
from typing import final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig


@dataclass(frozen=True)
class S3BucketResources:
    bucket: pulumi_aws.s3.Bucket


@final
class Bucket(Component[S3BucketResources], Linkable):
    def __init__(
        self,
        name: str,
        storage_class: str = "STANDARD",
        versioning_enabled: bool = False,
        public_access_block: bool = True,
        enforce_https: bool = True,
    ):
        super().__init__(name)
        self.storage_class = storage_class
        self.versioning_enabled = versioning_enabled
        self.public_access_block = public_access_block
        self.enforce_https = enforce_https
        self._resources = None

    def _create_resources(self) -> S3BucketResources:
        bucket = pulumi_aws.s3.Bucket(
            context().prefix(self.name),
            versioning={"enabled": self.versioning_enabled},
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

        pulumi.export(f"s3bucket_{self.name}_arn", bucket.arn)
        pulumi.export(f"s3bucket_{self.name}_name", bucket.bucket)
        pulumi.export(f"s3bucket_{self.name}_public_access_block_id", public_access_block.id)

        return S3BucketResources(bucket)

    @property
    def arn(self) -> pulumi.Output[str]:
        """Get the ARN of the S3 bucket."""
        return self.resources.bucket.arn

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


@dataclass(frozen=True)
class S3StaticWebsiteResources:
    bucket: Bucket
    files: list[pulumi_aws.s3.BucketObject]
    cloudfront_distribution: CloudFrontDistribution


@final
class S3StaticWebsite(Component[S3StaticWebsiteResources]):
    def __init__(self, name: str, directory: str, custom_domain: str):
        super().__init__(name)
        self.directory = directory
        self.custom_domain = custom_domain
        self._resources = None

    def _create_resources(self) -> S3StaticWebsiteResources:
        bucket = Bucket(
            # context().prefix(f"{self.name}-bucket"),
            f"{self.name}-bucket",
        )
        cloudfront_distribution = CloudFrontDistribution(
            name=f"{self.name}-cloudfront",
            s3_bucket=bucket.resources.bucket,
            custom_domain=self.custom_domain,
        )

        files = []
        ## glob all files in the directory
        for root, _, filenames in os.walk(self.directory):
            for filename in filenames:
                file_path = os.path.join(root, filename)  # noqa: PTH118
                key = os.path.relpath(file_path, self.directory)

                # Convert path separators and special chars to dashes,
                # ensure valid Pulumi resource name
                safe_key = re.sub(r"[^a-zA-Z0-9]", "-", key)
                # Remove consecutive dashes and leading/trailing dashes
                safe_key = re.sub(r"-+", "-", safe_key).strip("-")
                # resource_name = f"{self.name}-{safe_key}-{file_hash[:8]}"

                # DO NOT INCLUDE HASH IN RESOURCE NAME
                # If the resource name changes, Pulumi will treat it as a new resource, and create
                # a new s3 object
                # Then, the old one is deleted by pulumi. Sounds correct, but since the filename
                # (key) is the same, the delete operation deletes the new object!
                resource_name = f"{self.name}-{safe_key}"

                # For binary files, use source instead of content
                mimetype, _ = mimetypes.guess_type(filename)

                cache_control = "public, max-age=1"  # 1 second

                bucket_object = pulumi_aws.s3.BucketObject(
                    resource_name,
                    bucket=bucket.resources.bucket.id,
                    key=key,
                    source=pulumi.FileAsset(file_path),
                    content_type=mimetype,
                    cache_control=cache_control,
                )
                files.append(bucket_object)

        pulumi.export(f"s3_static_website_{self.name}_bucket_name", bucket.resources.bucket.bucket)
        pulumi.export(f"s3_static_website_{self.name}_bucket_arn", bucket.resources.bucket.arn)
        pulumi.export(
            f"s3_static_website_{self.name}_cloudfront_distribution_name",
            cloudfront_distribution.name,
        )
        pulumi.export(
            f"s3_static_website_{self.name}_cloudfront_domain_name",
            cloudfront_distribution.resources.distribution.domain_name,
        )
        pulumi.export(f"s3_static_website_{self.name}_custom_domain", self.custom_domain)
        pulumi.export(f"s3_static_website_{self.name}_files", [file.arn for file in files])

        return S3StaticWebsiteResources(
            bucket=bucket,
            files=files,
            cloudfront_distribution=cloudfront_distribution,
        )
