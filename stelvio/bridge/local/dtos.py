from dataclasses import dataclass
from typing import Any, final


@final
@dataclass(frozen=True)
class BridgeInvocationResult:
    success_result: Any | None
    error_result: Exception | None
    request_path: str
    request_method: str
    process_time_local: float
    status_code: int
