from dataclasses import dataclass
from typing import Literal, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig


@final
@dataclass(frozen=True)
class S3BucketResources:
    bucket: pulumi_aws.s3.Bucket
    public_access_block: pulumi_aws.s3.BucketPublicAccessBlock
    bucket_policy: pulumi_aws.s3.BucketPolicy | None


@final
class Bucket(Component[S3BucketResources], Linkable):
    def __init__(
        self, name: str, versioning: bool = False, access: Literal["public"] | None = None
    ):
        super().__init__(name)
        self.versioning = versioning
        self.access = access
        self._resources = None

    def _create_resources(self) -> S3BucketResources:
        bucket = pulumi_aws.s3.Bucket(
            context().prefix(self.name),
            bucket=context().prefix(self.name),
            versioning={"enabled": self.versioning},
        )

        # Configure public access block
        if self.access == "public":
            # setup readonly configuration
            public_access_block = pulumi_aws.s3.BucketPublicAccessBlock(
                context().prefix(f"{self.name}-pab"),
                bucket=bucket.id,
                block_public_acls=False,
                block_public_policy=False,
                ignore_public_acls=False,
                restrict_public_buckets=False,
            )
            public_read_policy = pulumi_aws.iam.get_policy_document(
                statements=[
                    {
                        "effect": "Allow",
                        "principals": [
                            {
                                "type": "*",
                                "identifiers": ["*"],
                            }
                        ],
                        "actions": ["s3:GetObject"],
                        "resources": [bucket.arn.apply(lambda arn: f"{arn}/*")],
                    }
                ]
            )
            bucket_policy = pulumi_aws.s3.BucketPolicy(
                context().prefix(f"{self.name}-policy"),
                bucket=bucket.id,
                policy=public_read_policy.json,
            )
            pulumi.export(f"s3bucket_{self.name}_policy_id", bucket_policy.id)
        else:
            public_access_block = pulumi_aws.s3.BucketPublicAccessBlock(
                context().prefix(f"{self.name}-pab"),
                bucket=bucket.id,
                block_public_acls=True,
                block_public_policy=True,
                ignore_public_acls=True,
                restrict_public_buckets=True,
            )
            bucket_policy = None

        pulumi.export(f"s3bucket_{self.name}_arn", bucket.arn)
        pulumi.export(f"s3bucket_{self.name}_name", bucket.bucket)
        pulumi.export(f"s3bucket_{self.name}_public_access_block_id", public_access_block.id)

        return S3BucketResources(bucket, public_access_block, bucket_policy)

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
