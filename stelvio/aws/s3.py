import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, final

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


@dataclass(frozen=True)
class S3StaticWebsiteResources:
    bucket: pulumi_aws.s3.Bucket
    files: list[pulumi_aws.s3.BucketObject]
    cloudfront_distribution: CloudFrontDistribution


@final
class S3StaticWebsite(Component[S3StaticWebsiteResources]):
    def __init__(self, name: str, custom_domain: str, directory: Path | str | None = None):
        super().__init__(name)
        self.directory = Path(directory) if isinstance(directory, str) else directory
        self.custom_domain = custom_domain
        self._resources = None

    def _create_resources(self) -> S3StaticWebsiteResources:
        # Validate directory exists
        if not self.directory.exists():
            raise FileNotFoundError(f"Directory does not exist: {self.directory}")

        bucket = Bucket(f"{self.name}-bucket")
        # Create CloudFront Function to handle directory index rewriting
        viewer_request_function = pulumi_aws.cloudfront.Function(
            context().prefix(f"{self.name}-viewer-request"),
            name=context().prefix(f"{self.name}-viewer-request-function"),
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
        cloudfront_distribution = CloudFrontDistribution(
            name=f"{self.name}-cloudfront",
            s3_bucket=bucket.resources.bucket,
            custom_domain=self.custom_domain,
            function_associations=[
                {
                    "event_type": "viewer-request",
                    "function_arn": viewer_request_function.arn,
                }
            ],
        )

        files = []
        # glob all files in the directory
        for root, _, filenames in os.walk(self.directory):
            for filename in filenames:
                root_path = Path(root)
                file_path = root_path / filename
                key = file_path.relative_to(self.directory)

                # Convert path separators and special chars to dashes,
                # ensure valid Pulumi resource name
                safe_key = re.sub(r"[^a-zA-Z0-9]", "-", str(key))
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
                    key=str(key),
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
            bucket=bucket.resources.bucket,
            files=files,
            cloudfront_distribution=cloudfront_distribution,
        )
