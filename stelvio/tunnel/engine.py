"""Pinned Pulumi command with a freshly captured, supervisor-owned AWS provider.

The selected engine attaches to that provider instead of spawning AWS plugins.
Both native processes are registered behind exec barriers before AWS work begins.
"""

from __future__ import annotations

import os
import select
import threading
from typing import TYPE_CHECKING

import grpc
from google.protobuf.empty_pb2 import Empty
from pulumi.automation import CommandResult, PulumiCommand
from pulumi.automation.errors import create_command_error
from pulumi.runtime.proto.provider_pb2_grpc import ResourceProviderStub
from semver import VersionInfo

from stelvio.tunnel.actors import AWS_VERSION, CLI_VERSION

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import TextIO

    from stelvio.tunnel.actors import ActorRegistry

_MAX_PORT = 65535


def _environment(additional: Mapping[str, str]) -> dict[str, str]:
    if not additional.get("AWS_ACCESS_KEY_ID") or not additional.get("AWS_SECRET_ACCESS_KEY"):
        raise RuntimeError("Native AWS actors require freshly captured static credentials")
    # Credential resolution and refresh stay in the supervisor. Child actors
    # cannot launch credential_process or choose an inherited handler profile.
    result = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
    result.update(additional)
    result.update(
        {
            "AWS_CONFIG_FILE": os.devnull,
            "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
            "AWS_PROFILE": "",
            "AWS_DEFAULT_PROFILE": "",
            "AWS_EC2_METADATA_DISABLED": "true",
            "PULUMI_AUTOMATION_API": "true",
        }
    )
    return result


def _consume(
    stream: TextIO, callback: Callable[[str], None] | None, chunks: list[str] | None
) -> None:
    for line in stream:
        value = line.rstrip()
        if callback:
            callback(value)
        if chunks is not None:
            chunks.append(value)
    stream.close()


class TrackedPulumiCommand(PulumiCommand):
    def __init__(self, registry: ActorRegistry) -> None:
        super().__init__(
            root=str(registry.cli.parent.parent), version=VersionInfo.parse(CLI_VERSION)
        )
        if str(self.version) != CLI_VERSION or self.command != str(registry.cli):
            raise RuntimeError("Native actor engine version differs from the proven profile")
        self.registry = registry

    def run(  # noqa: C901 - single guarded native operation with mandatory teardown
        self,
        args: list[str],
        cwd: str,
        additional_env: Mapping[str, str],
        on_output: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> CommandResult:
        self.registry.require_stopped()
        environment = _environment(additional_env)
        provider = self.registry.spawn("aws-provider", [], environment)
        consumers: list[threading.Thread] = []
        engine = None
        try:
            ready, _, _ = select.select([provider.stdout], [], [], 10)
            if not ready:
                raise RuntimeError("Registered AWS provider startup timed out")
            port = provider.stdout.readline().strip()
            if not port.isdecimal() or not 1 <= int(port) <= _MAX_PORT:
                raise RuntimeError("Registered AWS provider did not publish a valid port")
            for stream in (provider.stdout, provider.stderr):
                consumer = threading.Thread(
                    target=_consume, args=(stream, None, None), daemon=True
                )
                consumer.start()
                consumers.append(consumer)
            with grpc.insecure_channel("127.0.0.1:" + port) as channel:
                info = ResourceProviderStub(channel).GetPluginInfo(Empty(), timeout=5)
                if info.version != AWS_VERSION:
                    raise RuntimeError(
                        "Registered AWS provider version differs from the proven profile"
                    )
            environment["PULUMI_DEBUG_PROVIDERS"] = "aws:" + port
            # Preserve the SDK's noninteractive contract without mutating args.
            separator = args.index("--") if "--" in args else len(args)
            command_args = (
                args if "--non-interactive" in args[:separator] else ["--non-interactive", *args]
            )
            engine = self.registry.spawn("engine", command_args, environment, cwd)
            stdout: list[str] = []
            stderr: list[str] = []
            engine_consumers = []
            for stream, callback, chunks in (
                (engine.stdout, on_output, stdout),
                (engine.stderr, on_error, stderr),
            ):
                consumer = threading.Thread(target=_consume, args=(stream, callback, chunks))
                consumer.start()
                engine_consumers.append(consumer)
            for consumer in engine_consumers:
                consumer.join()
            engine.wait()
            result = CommandResult(
                stdout="\n".join(stdout), stderr="\n".join(stderr), code=engine.returncode
            )
            if result.code:
                raise create_command_error(result)
            return result
        finally:
            try:
                if engine:
                    self.registry.stop(engine)
            finally:
                self.registry.stop(provider)
            for consumer in consumers:
                consumer.join(timeout=5)
                if consumer.is_alive():
                    raise RuntimeError(
                        "Registered provider streams remain open; retain actor ownership"
                    )
            self.registry.require_stopped()
