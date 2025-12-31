from dataclasses import dataclass
from typing import final


@final
@dataclass(frozen=True)
class BridgeInvocationResult:
    success_result: dict | None
    error_result: Exception | None
    request_path: str
    request_method: str
    process_time_local: float
    status_code: int
    handler_name: str | None = None
