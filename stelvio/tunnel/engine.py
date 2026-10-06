"""Pinned Pulumi command with a freshly captured, supervisor-owned AWS provider.

The selected engine attaches to that provider instead of spawning AWS plugins.
Both native processes are registered behind exec barriers before AWS work begins.
"""

from __future__ import annotations

import os
import select
import threading
import time
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

    from stelvio.tunnel.access_program import CapturedCredentials
    from stelvio.tunnel.actors import ActorRegistry

_MAX_PORT = 65535
_MAX_PORT_DIGITS = 5
_ENGINE_SECONDS = 600
_STREAM_SECONDS = 5


def _environment(additional: Mapping[str, str]) -> dict[str, str]:
    if not additional.get("AWS_ACCESS_KEY_ID") or not additional.get("AWS_SECRET_ACCESS_KEY"):
        raise RuntimeError("Native AWS actors require freshly captured static credentials")
    # Credential resolution and refresh stay in the supervisor. Child actors
    # cannot launch credential_process or choose an inherited handler profile.
    result = {
        key: value for key, value in os.environ.items() if not key.startswith(("AWS_", "PULUMI_"))
    }
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
    stream: TextIO,
    callback: Callable[[str], None] | None,
    chunks: list[str] | None,
    failures: list[BaseException],
) -> None:
    try:
        for line in stream:
            value = line.rstrip()
            if callback:
                try:
                    callback(value)
                except BaseException as error:
                    failures.append(error)
                    callback = None
                    # Keep draining: closing a consumer after callback failure
                    # can block the engine on a full pipe and prevent teardown.
            if chunks is not None:
                chunks.append(value)
    except BaseException as error:
        failures.append(error)
    finally:
        stream.close()


def _provider_port(stream: TextIO) -> str:
    deadline = time.monotonic() + 10
    value = bytearray()
    while len(value) <= _MAX_PORT_DIGITS:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([stream], [], [], remaining)[0]:
            raise RuntimeError("Registered AWS provider startup timed out")
        byte = os.read(stream.fileno(), 1)
        if byte == b"\n":
            port = value.decode("ascii")
            if port.isdecimal() and 1 <= int(port) <= _MAX_PORT:
                return port
            break
        if not byte or byte not in b"0123456789":
            break
        value.extend(byte)
    raise RuntimeError("Registered AWS provider did not publish a valid port")


class TrackedPulumiCommand(PulumiCommand):
    def __init__(
        self,
        registry: ActorRegistry,
        *,
        provider_credentials: Callable[[], CapturedCredentials] | None = None,
    ) -> None:
        super().__init__(
            root=str(registry.cli.parent.parent), version=VersionInfo.parse(CLI_VERSION)
        )
        if str(self.version) != CLI_VERSION or self.command != str(registry.cli):
            raise RuntimeError("Native actor engine version differs from the proven profile")
        self.registry = registry
        self.provider_credentials = provider_credentials

    def _provider_environment(self, environment: dict[str, str]) -> dict[str, str]:
        provider_environment = environment
        if self.provider_credentials is not None:
            captured = self.provider_credentials()
            intent = self.registry.journal.intent
            if captured.account != intent.account or captured.region != intent.region:
                raise RuntimeError(
                    "Native AWS provider credential context changed; creation refused"
                )
            provider_environment = _environment(
                {
                    "AWS_ACCESS_KEY_ID": captured.access_key,
                    "AWS_SECRET_ACCESS_KEY": captured.secret_key,
                    "AWS_SESSION_TOKEN": captured.token or "",
                    "AWS_REGION": captured.region,
                    "AWS_DEFAULT_REGION": captured.region,
                }
            )
        provider_environment = dict(provider_environment)
        provider_environment.pop("PULUMI_CONFIG_PASSPHRASE", None)
        return provider_environment

    def run(  # noqa: C901 - ordered native execution and mandatory actor teardown
        self,
        args: list[str],
        cwd: str,
        additional_env: Mapping[str, str],
        on_output: Callable[[str], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> CommandResult:
        self.registry.require_stopped()
        environment = _environment(additional_env)
        provider_environment = self._provider_environment(environment)
        provider = self.registry.spawn("aws-provider", [], provider_environment)
        consumers: list[threading.Thread] = []
        engine_consumers: list[threading.Thread] = []
        failures: list[BaseException] = []
        engine = None
        try:
            port = _provider_port(provider.stdout)
            for stream in (provider.stdout, provider.stderr):
                consumer = threading.Thread(
                    target=_consume, args=(stream, None, None, failures), daemon=True
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
            for stream, callback, chunks in (
                (engine.stdout, on_output, stdout),
                (engine.stderr, on_error, stderr),
            ):
                consumer = threading.Thread(
                    target=_consume, args=(stream, callback, chunks, failures), daemon=True
                )
                consumer.start()
                engine_consumers.append(consumer)
            engine.wait(timeout=_ENGINE_SECONDS)
            for consumer in engine_consumers:
                consumer.join(timeout=_STREAM_SECONDS)
                if consumer.is_alive():
                    raise RuntimeError("Registered engine streams remain open; retain ownership")
            if failures:
                raise failures[0]
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
            for consumer in consumers + engine_consumers:
                consumer.join(timeout=_STREAM_SECONDS)
                if consumer.is_alive():
                    raise RuntimeError(
                        "Registered actor streams remain open; retain actor ownership"
                    )
            self.registry.require_stopped()
