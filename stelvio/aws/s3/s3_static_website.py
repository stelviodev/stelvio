import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.aws.function.function import _extract_links_permissions
from stelvio.aws.function.iam import (
    _attach_role_policies,
    _create_function_policy,
    _create_lambda_role,
)
from stelvio.aws.s3.s3 import Bucket
from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.bridge.local.handlers import WebsocketHandlers
from stelvio.bridge.remote.infrastructure import (
    _create_lambda_bridge_archive,
    discover_or_create_appsync,
)
from stelvio.component import BridgeableComponent, Component, safe_name


@final
@dataclass(frozen=True)
class S3StaticWebsiteResources:
    bucket: pulumi_aws.s3.Bucket
    _function_resource: pulumi_aws.lambda_.Function | None
    _function_resource_url: pulumi_aws.lambda_.FunctionUrl | None
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
    directory: Path | None = None
    command: str | None = None
    env_vars: dict[str, str] | None = None
    working_directory: Path | None = None


@final
class S3StaticWebsite(Component[S3StaticWebsiteResources], BridgeableComponent):
    def __init__(   # noqa: PLR0913
        self,
        name: str,
        custom_domain: str | None = None,
        # directory: Path | str | None = None,
        default_cache_ttl: int = 120,
        build_options: dict | StaticWebsiteBuildOptions | None = None,
        dev_options: dict | StaticWebsiteDevOptions | None = None,
        create_distribution: bool = True,
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
        self.create_distribution = create_distribution
        self._resources = None
        self._dev_endpoint_id = f"{self.name}-{sha256(uuid.uuid4().bytes).hexdigest()[:8]}"

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

        if context().dev_mode:
            files = []
            self.process_dev_options()
            appsync_bridge = discover_or_create_appsync(
                region=context().aws.region, profile=context().aws.profile
            )

            function_name = safe_name(context().prefix(), f"{self.name}-stub", 64)
            lambda_role = _create_lambda_role(self.name)
            iam_statements = _extract_links_permissions([])
            function_policy = _create_function_policy(self.name, iam_statements)
            role_attachments = _attach_role_policies(self.name, lambda_role, function_policy)

            env_vars = {}
            WebsocketHandlers.register(self)
            env_vars["STLV_APPSYNC_REALTIME"] = appsync_bridge.realtime_endpoint
            env_vars["STLV_APPSYNC_HTTP"] = appsync_bridge.http_endpoint
            env_vars["STLV_APPSYNC_API_KEY"] = appsync_bridge.api_key
            env_vars["STLV_APP_NAME"] = context().name
            env_vars["STLV_STAGE"] = context().env
            env_vars["STLV_FUNCTION_NAME"] = self.name
            env_vars["STLV_DEV_ENDPOINT_ID"] = self._dev_endpoint_id
            function_resource = pulumi_aws.lambda_.Function(
                function_name,
                role=lambda_role.arn,
                architectures=["x86_64"],
                runtime="python3.12",
                code=_create_lambda_bridge_archive(),
                handler="stlv_function_stub.handler",
                environment={"variables": env_vars},
                memory_size=128,
                timeout=60,
                # layers=[layer.arn for layer in self.config.layers]
                # if self.config.layers else None,
                layers=None,
                # Technically this is necessary only for tests as otherwise
                # it's ok if role attachments are created after functions
                opts=pulumi.ResourceOptions(depends_on=role_attachments),
            )

            function_url = pulumi_aws.lambda_.FunctionUrl(
                safe_name(context().prefix(), f"{self.name}-stub-url", 64),
                function_name=function_resource.name,
                authorization_type="AWS_IAM",
            )
            function_resource.function_url = function_url.function_url

            pulumi.export(
                f"s3_static_website_{self.name}_stub_function_name", function_resource.name
            )
            pulumi.export(
                f"s3_static_website_{self.name}_stub_function_url_name", function_url.function_url
            )

            bucket = None
        else:
            function_url = None
            files = self._process_build_options(bucket)
            function_resource = None

        cloudfront_distribution = None
        if self.create_distribution:
            cloudfront_distribution = CloudFrontDistribution(
                name=f"{self.name}-cloudfront",
                bucket=bucket,
                _function_resource=function_resource,
                custom_domain=self.custom_domain,
                function_associations=[
                    {
                        "event_type": "viewer-request",
                        "function_arn": viewer_request_function.arn,
                    }
                ]
                if not context().dev_mode
                else [],
            )

        # Upload files from directory to S3 bucket
        # files = self._process_build_options(bucket)
        if bucket:
            pulumi.export(
                f"s3_static_website_{self.name}_bucket_name", bucket.resources.bucket.bucket
            )
            pulumi.export(f"s3_static_website_{self.name}_bucket_arn", bucket.resources.bucket.arn)
        if cloudfront_distribution:
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
            bucket=bucket.resources.bucket if bucket else None,
            _function_resource=function_resource,
            _function_resource_url=function_url,
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

    def process_dev_options(self) -> None:
        if self.dev_options is None:
            return

        if self.dev_options.command is not None:

            def _run_dev_command() -> None:
                # Execute dev command
                env = os.environ.copy()
                if self.dev_options.env_vars:
                    env.update(self.dev_options.env_vars)

                subprocess.run( # noqa: S602
                    self.dev_options.command,
                    shell=True,
                    cwd=str(self.dev_options.working_directory or Path.cwd()),
                    env=env,
                    check=False,
                )

            thread = threading.Thread(target=_run_dev_command, daemon=False)
            thread.start()

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

            subprocess.run(  # noqa: S602
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

    def _proxy_http(self, method: str, path: str, headers: dict, body: str) -> dict:
        import requests

        url = f"http://localhost:{self.dev_options.port}{path}"
        response = requests.request(method, url, headers=headers, data=body, timeout=10)
        return {
            "statusCode": response.status_code,
            "headers": dict(response.headers),
            "body": response.text,
        }

    def _proxy_file(self, path: str) -> dict:
        if self.dev_options.directory is None:
            raise RuntimeError(
                "Directory is not configured in dev options for this S3StaticWebsite."
            )

        file_path = self.dev_options.directory / path.lstrip("/")
        if file_path.is_dir():
            file_path = file_path / "index.html"
        if not file_path.exists() or not file_path.is_file():
            return {
                "statusCode": 404,
                "body": "Not Found",
            }
        with Path.open(file_path, encoding="utf-8") as f:
            content = f.read()
        return {
            "statusCode": 200,
            "body": content,
        }

    async def _handle_bridge_event(self, event: dict) -> dict:
        if self.dev_options is None:
            raise RuntimeError(f"Dev options are not configured for Static Website {self.name}.")

        if not self.dev_options.port and not self.dev_options.directory:
            raise RuntimeError(
                f"Neither port nor directory is configured in dev options for Static Website "
                f"{self.name}."
            )

        if self.dev_options.port and self.dev_options.directory:
            raise RuntimeError(
                f"Both port and directory are configured in dev options for Static Website "
                f"{self.name}. Please configure only one."
            )

        lambda_event = event.get("event", "{}")
        lambda_event = json.loads(lambda_event) if isinstance(lambda_event, str) else lambda_event

        start_time = time.perf_counter()
        if self.dev_options.port:
            result = self._proxy_http(
                method=lambda_event["event"]["requestContext"]["http"]["method"],
                path=lambda_event["event"]["requestContext"]["http"]["path"],
                headers=lambda_event["event"].get("headers", {}),
                body=lambda_event["event"].get("body", ""),
            )
        if self.dev_options.directory:
            result = self._proxy_file(
                path=lambda_event["event"]["requestContext"]["http"]["path"],
            )
        end_time = time.perf_counter()
        run_time = end_time - start_time

        return BridgeInvocationResult(
            success_result=result,
            error_result=None,
            process_time_local=float(run_time * 1000),
            request_path=lambda_event["event"]["requestContext"]["http"]["path"],
            request_method=lambda_event["event"]["requestContext"]["http"]["method"],
            status_code=result["statusCode"],
            handler_name=f"S3StaticWebsite:{self.name}",
        )
