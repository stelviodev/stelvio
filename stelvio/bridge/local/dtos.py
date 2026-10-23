from dataclasses import dataclass
from pathlib import Path
from typing import final


@dataclass(frozen=True)
class DurableCleanupRequest:
    root: Path
    profile: str | None
    region: str
    execution_arn: str


@final
@dataclass(frozen=True)
class BridgeInvocationResult:
    success_result: dict | None
    error_result: BaseException | None  # SystemExit from a handler is reported, not obeyed
    request_path: str
    request_method: str
    process_time_local: float
    status_code: int
    handler_name: str | None = None
    durable_execution_arn: str | None = None
    durable_status: str | None = None
    durable_cleanup: DurableCleanupRequest | None = None
