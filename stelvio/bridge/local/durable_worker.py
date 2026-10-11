"""One durable invocation per process; AWS's SDK owns checkpointing and replay."""

from __future__ import annotations

import importlib
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from stelvio.bridge.local.lambda_context import LambdaContext


def main() -> None:
    request = json.loads(sys.stdin.read())
    result_path = Path(request["result_path"])
    response: dict[str, Any]
    try:
        result = _invoke(request)
        response = {"success": True, "result": result}
    except BaseException as exc:  # SystemExit must terminate this invocation, not the dev server.
        hint = _credential_hint(str(exc))
        response = {
            "success": False,
            "error": str(exc) + hint,
            "errorType": type(exc).__name__,
            "stackTrace": traceback.format_exc(),
        }
    result_path.write_text(json.dumps(response))


def _invoke(request: dict[str, Any]) -> dict[str, Any]:
    # Defer the SDK import so a missing local dependency produces an actionable bridge error.
    try:
        import aws_durable_execution_sdk_python  # noqa: F401, PLC0415
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            "Local durable execution requires aws-durable-execution-sdk-python. "
            "Install it in the Python environment running stlv dev and declare it "
            "in the function's requirements."
        ) from exc
    package = Path(request["package"])
    sys.path.insert(0, str(package))
    module_name = Path(request["handler_file"]).with_suffix("").as_posix().replace("/", ".")
    module = importlib.import_module(module_name)
    handler = getattr(module, request["handler_name"])
    result = handler(request["event"], LambdaContext(**request["context"]))
    if not isinstance(result, dict) or result.get("Status") not in (
        "PENDING",
        "SUCCEEDED",
        "FAILED",
        "RETRY",
    ):
        raise ValueError(
            "Durable handler must use @durable_execution and return the SDK status envelope."
        )
    hint = _credential_hint(json.dumps(result.get("Error", {})))
    if hint:
        sys.stderr.write(hint + "\n")
    return result


def _credential_hint(message: str) -> str:
    if any(
        word in message
        for word in (
            "AccessDenied",
            "NoCredentials",
            "Unable to locate credentials",
            "ExpiredToken",
        )
    ):
        return (
            " Local durable execution uses your configured AWS credentials. "
            "Refresh them and grant lambda:CheckpointDurableExecution and "
            "lambda:GetDurableExecutionState on this function's qualified execution ARNs."
        )
    return ""


if __name__ == "__main__":
    main()
