"""No-AWS-resource proof of supervisor-owned, prestarted AWS provider attachment.

Preview uses a disposable file backend, fake credentials and no data sources.
The provider's PID/birth identity exists before the engine can configure it.
This is a supplemental candidate proof, not production process containment.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import tempfile
from importlib.metadata import version
from pathlib import Path

import grpc
import pulumi
import pulumi_aws as aws
from google.protobuf.empty_pb2 import Empty
from pulumi.automation import (
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    PulumiCommand,
    create_or_select_stack,
)
from pulumi.runtime.proto.provider_pb2_grpc import ResourceProviderStub
from semver import VersionInfo

from stelvio.pulumi import get_stelvio_config_dir
from stelvio.tunnel.processes import identity

AWS_VERSION = "7.47.0"


def main() -> None:
    root = get_stelvio_config_dir()
    binary = root / ".pulumi" / "plugins" / f"resource-aws-v{AWS_VERSION}" / "pulumi-resource-aws"
    environment = {
        "AWS_ACCESS_KEY_ID": "fake-preview-key",
        "AWS_SECRET_ACCESS_KEY": "fake-preview-secret",
        "AWS_SESSION_TOKEN": "",
        "AWS_PROFILE": "",
        "AWS_DEFAULT_PROFILE": "",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_EC2_METADATA_DISABLED": "true",
        "PULUMI_CONFIG_PASSPHRASE": "disposable-local-preview",
    }
    provider = subprocess.Popen(  # noqa: S603 - pinned installed native provider, no shell
        [str(binary)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ | environment,
    )
    try:
        ready, _, _ = select.select([provider.stdout], [], [], 10)
        if not ready:
            raise RuntimeError("Provider did not publish its port")
        port = provider.stdout.readline().strip()
        if not port.isdigit():
            raise RuntimeError("Provider startup failed: " + provider.stderr.read(2000))
        original = identity(provider.pid)
        with grpc.insecure_channel("127.0.0.1:" + port) as channel:
            info = ResourceProviderStub(channel).GetPluginInfo(Empty(), timeout=5)
            if info.version != AWS_VERSION:
                raise RuntimeError("Unexpected standalone provider version")
        environment["PULUMI_DEBUG_PROVIDERS"] = "aws:" + port

        def program() -> None:
            scoped = aws.Provider(
                "preview-aws",
                region="us-east-1",
                access_key="fake-preview-key",
                secret_key="fake-preview-secret",  # noqa: S106 - deliberately fake preview credentials
                skip_credentials_validation=True,
                skip_requesting_account_id=True,
                skip_metadata_api_check=True,
                skip_region_validation=True,
            )
            aws.ec2.SecurityGroup(
                "access-preview",
                vpc_id="vpc-not-created",
                description="Preview only; never deployed",
                opts=pulumi.ResourceOptions(provider=scoped),
            )

        with tempfile.TemporaryDirectory(prefix="stelvio-provider-attach-") as scratch:
            backend = Path(scratch).as_uri()
            stack = create_or_select_stack(
                stack_name="attach-proof",
                project_name="stelvio-provider-attach",
                program=program,
                opts=LocalWorkspaceOptions(
                    pulumi_command=PulumiCommand(
                        root=str(root), version=VersionInfo.parse(version("pulumi"))
                    ),
                    pulumi_home=str(root / ".pulumi"),
                    project_settings=ProjectSettings(
                        name="stelvio-provider-attach",
                        runtime="python",
                        backend=ProjectBackend(backend),
                    ),
                    env_vars=environment,
                ),
            )
            result = stack.preview()
            current = identity(provider.pid)
            if not current or not original.same_process(current) or provider.poll() is not None:
                raise RuntimeError("Supervisor-owned provider disappeared after preview")
            print(  # noqa: T201 - concise proof evidence
                json.dumps(
                    {
                        "attached_provider": info.version,
                        "changes": result.change_summary,
                        "provider_alive_after_engine": True,
                    }
                )
            )
    finally:
        provider.kill()
        provider.wait(timeout=5)
        provider.stdout.close()
        provider.stderr.close()


if __name__ == "__main__":
    main()
