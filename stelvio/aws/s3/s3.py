from dataclasses import dataclass
from typing import Any, Literal, TypedDict, final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.component import Component, link_config_creator
from stelvio.link import LinkableMixin, LinkConfig


@final
@dataclass(frozen=True)
class S3BucketResources:
    bucket: pulumi_aws.s3.Bucket
    public_access_block: pulumi_aws.s3.BucketPublicAccessBlock
    bucket_policy: pulumi_aws.s3.BucketPolicy | None


class S3BucketCustomizationDict(TypedDict, total=False):
    bucket: pulumi_aws.s3.BucketArgs | dict[str, Any] | None
    public_access_block: pulumi_aws.s3.BucketPublicAccessBlockArgs | dict[str, Any] | None
    bucket_policy: pulumi_aws.s3.BucketPolicyArgs | dict[str, Any] | None


@final
class Bucket(Component[S3BucketResources, S3BucketCustomizationDict], LinkableMixin):
    def __init__(
        self,
        name: str,
        versioning: bool = False,
        access: Literal["public"] | None = None,
        customize: S3BucketCustomizationDict | None = None,
    ):
        super().__init__(name, customize=customize)
        self.versioning = versioning
        self.access = access
        self._resources = None

    def _create_resources(self) -> S3BucketResources:
        bucket = pulumi_aws.s3.Bucket(
            context().prefix(self.name),
            **self._customizer(
                "bucket",
                {
                    "bucket": context().prefix(self.name),
                    "versioning": {"enabled": self.versioning},
                },
            ),
        )

        # Configure public access block
        if self.access == "public":
            # setup readonly configuration
            public_access_block = pulumi_aws.s3.BucketPublicAccessBlock(
                context().prefix(f"{self.name}-pab"),
                **self._customizer(
                    "public_access_block",
                    {
                        "bucket": bucket.id,
                        "block_public_acls": False,
                        "block_public_policy": False,
                        "ignore_public_acls": False,
                        "restrict_public_buckets": False,
                    },
                ),
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


@link_config_creator(Bucket)
def default_bucket_link(bucket_component: Bucket) -> LinkConfig:
    bucket = bucket_component.resources.bucket
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
