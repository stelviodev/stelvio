import mimetypes
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.aws.s3.s3 import Bucket
from stelvio.component import Component, safe_name


@final
@dataclass(frozen=True)
class S3StaticWebsiteResources:
    bucket: pulumi_aws.s3.Bucket
    files: list[pulumi_aws.s3.BucketObject]
    cloudfront_distribution: CloudFrontDistribution


REQUEST_INDEX_HTML_FUNCTION_JS = """
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
"""


@final
@dataclass(frozen=True)
class StaticWebsiteBuildOptions:
    directory: Path | None = None
    command: str | None = None
    env_vars: dict[str, str] | None = None
    working_directory: Path | None = None


@final
@dataclass(frozen=True)
class StaticWebsiteDevOptions:
    port: int | None = None
    command: str | None = None
    env_vars: dict[str, str] | None = None
    working_directory: Path | None = None


@final
class S3StaticWebsite(Component[S3StaticWebsiteResources]):
    def __init__(
        self,
        name: str,
        custom_domain: str | None = None,
        # directory: Path | str | None = None,
        default_cache_ttl: int = 120,
        build_options: dict | StaticWebsiteBuildOptions | None = None,
        dev_options: dict | StaticWebsiteDevOptions | None = None,
    ):
        super().__init__(name)
        # self.directory = Path(directory) if isinstance(directory, str) else directory
        self.custom_domain = custom_domain
        self.default_cache_ttl = default_cache_ttl
        if isinstance(build_options, dict):
            build_options = StaticWebsiteBuildOptions(**build_options)
        if isinstance(dev_options, dict):
            dev_options = StaticWebsiteDevOptions(**dev_options)
        self.build_options = build_options
        self.dev_options = dev_options
        self._resources = None

    def _create_resources(self) -> S3StaticWebsiteResources:
        bucket_name = f"{self.name}-bucket"
        bucket = Bucket(bucket_name)
        # Create CloudFront Function to handle directory index rewriting
        viewer_request_function = pulumi_aws.cloudfront.Function(
            context().prefix(f"{self.name}-viewer-request"),
            name=context().prefix(f"{self.name}-viewer-request-function"),
            runtime="cloudfront-js-1.0",
            comment="Rewrite requests to directories to serve index.html",
            code=REQUEST_INDEX_HTML_FUNCTION_JS,  # TODO: (configurable?)
        )
        cloudfront_distribution = CloudFrontDistribution(
            name=f"{self.name}-cloudfront",
            bucket=bucket,
            custom_domain=self.custom_domain,
            function_associations=[
                {
                    "event_type": "viewer-request",
                    "function_arn": viewer_request_function.arn,
                }
            ],
        )

        # Upload files from directory to S3 bucket
        files = self._process_build_options(bucket)

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

    def _create_s3_bucket_object(
        self, bucket: Bucket, directory: Path, file_path: Path
    ) -> pulumi_aws.s3.BucketObject:
        key = file_path.relative_to(directory)

        # Convert path separators and special chars to dashes,
        # ensure valid Pulumi resource name
        safe_key = re.sub(r"[^a-zA-Z0-9]", "-", str(key))
        # Remove consecutive dashes and leading/trailing dashes
        safe_key = re.sub(r"-+", "-", safe_key).strip("-")
        # resource_name = f"{self.name}-{safe_key}-{file_hash[:8]}"

        # DO NOT INCLUDE HASH IN RESOURCE NAME
        # If the resource name changes, Pulumi will treat it as a new resource,
        # and create a new s3 object
        # Then, the old one is deleted by pulumi. Sounds correct, but since the
        # filename (key) is the same, the delete operation deletes the new object!
        resource_name = f"{self.name}-{safe_key}"

        # For binary files, use source instead of content
        mimetype, _ = mimetypes.guess_type(file_path.name)

        cache_control = f"public, max-age={self.default_cache_ttl}"

        return pulumi_aws.s3.BucketObject(
            safe_name(context().prefix(), resource_name, 128, "-p"),
            bucket=bucket.resources.bucket.id,
            key=str(key),
            source=pulumi.FileAsset(file_path),
            content_type=mimetype,
            cache_control=cache_control,
        )

    def _process_build_options(
        self,
        bucket: Bucket,
    ) -> list[pulumi_aws.s3.BucketObject]:
        if self.build_options is None:
            return []

        if self.build_options.command is not None:
            # Execute build command
            env = os.environ.copy()
            if self.build_options.env_vars:
                env.update(self.build_options.env_vars)

            subprocess.run( # noqa: S602
                self.build_options.command,
                shell=True,
                check=True,
                cwd=str(self.build_options.working_directory or Path.cwd()),
                env=env,
            )

        directory = self.build_options.directory

        # glob all files in the directory
        if directory is None:
            return []

        return [
            self._create_s3_bucket_object(bucket, directory, file_path)
            for file_path in directory.rglob("*")
            if file_path.is_file()
        ]
