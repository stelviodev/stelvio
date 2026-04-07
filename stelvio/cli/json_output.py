"""JSON and newline-delimited stream output for CLI commands."""

import json
import sys
from collections.abc import Callable
from datetime import datetime
from typing import TypedDict, Unpack

from rich.console import Console

from stelvio.rich_deployment_handler import RichDeploymentHandler


class JsonSummaryKwargs(TypedDict, total=False):
    status: str
    outputs: dict[str, object] | None
    exit_code: int
    fallback_error: str | None
    message: str | None


def write_json_line(data: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def stream_timestamp() -> str:
    return datetime.now().astimezone().isoformat()


def stream_writer() -> Callable[[dict[str, object]], None]:
    return write_json_line


def emit_stream_start(operation: str, app_name: str, env: str) -> None:
    write_json_line(
        {
            "event": "start",
            "operation": operation,
            "app": app_name,
            "env": env,
            "timestamp": stream_timestamp(),
        }
    )


def print_json_summary(
    console: Console, handler: RichDeploymentHandler, **summary_kwargs: Unpack[JsonSummaryKwargs]
) -> None:
    console.print_json(data=handler.build_json_summary(**summary_kwargs))


def print_stream_summary(
    handler: RichDeploymentHandler, **summary_kwargs: Unpack[JsonSummaryKwargs]
) -> None:
    payload = handler.build_json_summary(**summary_kwargs)
    payload["event"] = "summary"
    payload["timestamp"] = stream_timestamp()
    write_json_line(payload)


def print_json_error(  # noqa: PLR0913
    console: Console, *, operation: str, app_name: str, env: str, error: str, exit_code: int = 1
) -> None:
    console.print_json(
        data={
            "operation": operation,
            "app": app_name,
            "env": env,
            "timestamp": stream_timestamp(),
            "status": "failed",
            "exit_code": exit_code,
            "errors": [{"message": error}],
        }
    )


def print_stream_error(
    *, operation: str, app_name: str, env: str, error: str, exit_code: int = 1
) -> None:
    write_json_line(
        {
            "event": "error",
            "operation": operation,
            "app": app_name,
            "env": env,
            "timestamp": stream_timestamp(),
            "status": "failed",
            "exit_code": exit_code,
            "errors": [{"message": error}],
        }
    )
