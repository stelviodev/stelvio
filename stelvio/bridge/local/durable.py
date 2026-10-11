"""Persistent source snapshots and isolated workers for AWS-backed durable dev calls."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config

from stelvio.aws.function.packaging import _folder_assets, _link_file_sources
from stelvio.aws.function.resources_codegen import create_stlv_resource_file_content
from stelvio.bridge.local.dtos import BridgeInvocationResult
from stelvio.project import get_dot_stelvio_dir, get_project_root

if TYPE_CHECKING:
    from stelvio.aws.function import Function

logger = logging.getLogger(__name__)
_RETURN_RESERVE_SECONDS = 3
_TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "STOPPED", "TIMED_OUT"}


class DurableInvocationError(RuntimeError):
    """An invocation failed before the SDK returned its durable status envelope."""


def _is_replay(event: dict[str, Any]) -> bool:
    state = event.get("InitialExecutionState", {})
    return bool(
        state.get("NextMarker")
        or any(op.get("Type") != "EXECUTION" for op in state.get("Operations", []))
    )


def _snapshot(function: Function, event: dict[str, Any], environment: dict[str, str]) -> Path:
    execution_arn = event["DurableExecutionArn"]
    directory = get_dot_stelvio_dir() / "durable-dev" / sha256(execution_arn.encode()).hexdigest()
    if (directory / "manifest.json").is_file():
        return directory
    if _is_replay(event):
        raise DurableInvocationError(
            f"Source snapshot missing for durable replay {execution_arn}. "
            "Keep .stelvio/durable-dev across restarts; start a new execution to use current code."
        )
    directory.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=directory.parent))
    try:
        manifest = _populate_snapshot(function, temporary, directory, environment, execution_arn)
        (temporary / "manifest.json").write_text(json.dumps(manifest))
        try:
            temporary.rename(directory)
        except OSError:
            if not (directory / "manifest.json").is_file():
                raise
        return directory
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _populate_snapshot(
    function: Function,
    temporary: Path,
    directory: Path,
    environment: dict[str, str],
    execution_arn: str,
) -> dict[str, Any]:
    package = temporary / "package"
    package.mkdir()
    config = function.config
    handler_file = Path(config.handler_file_path).with_suffix(".py")
    if config.folder_path:
        sources = {
            destination: Path(asset.path)
            for destination, asset in _folder_assets(
                get_project_root() / config.folder_path, config.folder_path
            ).items()
        }
    else:
        sources = {handler_file.name: get_project_root() / handler_file}
        handler_file = Path(handler_file.name)
    linked = _link_file_sources(function.name, config.links)
    for destination in linked:
        if any(
            destination == source
            or destination.startswith(source + "/")
            or source.startswith(destination + "/")
            for source in [*sources, "stlv_resources.py"]
        ):
            raise ValueError(f"Linked file {destination!r} conflicts with the handler package")
    for destination, source in (sources | linked).items():
        target = package / destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    if not (package / handler_file).is_file():
        raise FileNotFoundError(f"Durable handler file not found: {handler_file}")
    # Import here to avoid the Function -> durable bridge -> Function registry cycle.
    from stelvio.aws.function.function import (  # noqa: PLC0415
        FunctionEnvVarsRegistry,
        _extract_links_property_mappings,
    )

    content = create_stlv_resource_file_content(
        _extract_links_property_mappings(config.links),
        "STLV_CORS_ALLOW_ORIGIN" in FunctionEnvVarsRegistry.get_env_vars(function),
    )
    if content is not None:
        (package / "stlv_resources.py").write_text(content)
    # Links that refer to local asset paths must read the pinned asset too.
    replacements = {
        str(source): str(directory / "package" / destination)
        for destination, source in linked.items()
    }
    pinned_environment = {}
    for name, original_value in environment.items():
        value = original_value
        if name.startswith("AWS_"):
            continue  # Resolve credentials/region anew; never persist credentials.
        for source, destination in replacements.items():
            value = value.replace(source, destination)
        pinned_environment[name] = value
    return {
        "execution_arn": execution_arn,
        "handler_file": handler_file.as_posix(),
        "handler_name": config.handler_function_name,
        "environment": pinned_environment,
    }


def _cleanup_snapshots(root: Path, environment: dict[str, str], current_arn: str) -> None:
    """Only AWS terminal status permits removal; missing permission retains the snapshot."""
    manifests = list(root.glob("*/manifest.json"))
    if not manifests:
        return
    session = boto3.Session(
        profile_name=environment.get("AWS_PROFILE"), region_name=environment.get("AWS_REGION")
    )
    client = session.client(
        "lambda", config=Config(connect_timeout=1, read_timeout=1, retries={"max_attempts": 0})
    )
    for path in manifests:
        manifest = json.loads(path.read_text())
        arn = manifest["execution_arn"]
        if arn == current_arn:
            continue
        try:
            response = client.get_durable_execution(DurableExecutionArn=arn)
        except Exception:  # Cleanup must never break a pending execution.
            logger.debug("Unable to confirm durable execution status for cleanup", exc_info=True)
            continue
        if response.get("Status") in _TERMINAL_STATUSES:
            shutil.rmtree(path.parent)


async def _run_worker(
    function: Function,
    snapshot: Path,
    manifest: dict[str, Any],
    environment: dict[str, str],
    message: dict[str, Any],
) -> dict[str, Any]:
    event = message["event"]
    child_environment = {**environment, **manifest["environment"]}
    child_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    child_environment["PYTHONUNBUFFERED"] = "1"
    child_environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
    child_environment["AWS_LAMBDA_FUNCTION_NAME"] = function.name
    context_data = message["context"]
    invoked_arn = context_data.get("invoked_function_arn") or ""
    child_environment["AWS_LAMBDA_FUNCTION_VERSION"] = invoked_arn.rsplit(":", 1)[-1]
    deadline = context_data["epoch_deadline_time_in_ms"] / 1000
    timeout = deadline - time.time() - _RETURN_RESERVE_SECONDS
    if timeout <= 0:
        raise TimeoutError("Durable invocation has no time remaining for local execution")
    with tempfile.TemporaryDirectory(prefix="stlv-durable-result-") as result_directory:
        result_path = Path(result_directory) / "result.json"
        request = {
            "package": str(snapshot / "package"),
            "handler_file": manifest["handler_file"],
            "handler_name": manifest["handler_name"],
            "event": event,
            "context": context_data,
            "result_path": str(result_path),
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "stelvio.bridge.local.durable_worker",
            stdin=asyncio.subprocess.PIPE,
            env=child_environment,
            cwd=snapshot / "package",
        )
        try:
            await asyncio.wait_for(process.communicate(json.dumps(request).encode()), timeout)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode != 0 or not result_path.is_file():
            raise DurableInvocationError(
                f"Local durable worker exited without a response (exit {process.returncode})"
            )
        response = json.loads(result_path.read_text())
        if not response["success"]:
            raise DurableInvocationError(
                f"{response['errorType']}: {response['error']}\n{response['stackTrace']}"
            )
        return response["result"]


def _validate_invocation(event: dict[str, Any], execution_arn: str) -> None:
    if not isinstance(execution_arn, str) or execution_arn == "unknown":
        raise DurableInvocationError("Missing DurableExecutionArn in durable bridge invocation")
    if not isinstance(event.get("CheckpointToken"), str):
        raise DurableInvocationError("Missing CheckpointToken in durable bridge invocation")


async def handle_durable_invocation(function: Function, data: dict) -> BridgeInvocationResult:
    raw = data["event"]
    message = json.loads(raw) if isinstance(raw, str) else raw
    event = message.get("event", {})
    started = time.perf_counter()
    execution_arn = event.get("DurableExecutionArn", "unknown")
    result = None
    error = None
    process_environment = dict(data.get("_local_environment", os.environ))
    try:
        _validate_invocation(event, execution_arn)
        environment = await function._get_environment_for_bridge_event()  # noqa: SLF001
        # Packaging/link helpers construct Pulumi objects and require the event-loop thread.
        snapshot = _snapshot(function, event, environment)
        manifest = json.loads((snapshot / "manifest.json").read_text())
        result = await _run_worker(
            function, snapshot, manifest, {**process_environment, **environment}, message
        )
        # The current envelope has not reached AWS yet, so only collect other confirmed
        # terminal executions. The current snapshot is checked on a later invocation.
        try:
            await asyncio.wait_for(
                asyncio.to_thread(_cleanup_snapshots, snapshot.parent, environment, execution_arn),
                timeout=1,
            )
        except Exception:
            logger.debug("Durable snapshot cleanup deferred", exc_info=True)
    except (Exception, SystemExit) as exc:
        error = exc
    return BridgeInvocationResult(
        success_result=result,
        error_result=error,
        request_path=execution_arn,
        request_method="DURABLE",
        process_time_local=(time.perf_counter() - started) * 1000,
        status_code=-1,
        handler_name=function.config.handler,
        durable_execution_arn=execution_arn,
        durable_status=result.get("Status") if result is not None else None,
    )
