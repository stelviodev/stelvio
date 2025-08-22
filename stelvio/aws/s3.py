from dataclasses import dataclass
from typing import final

import pulumi
import pulumi_aws

from stelvio import context
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
