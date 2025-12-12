from dataclasses import dataclass
from typing import Any, final


@final
@dataclass(frozen=True)
class BridgeInvocationResult:
    success_result: Any | None
    error_result: Any | None
    request_path: str
    process_time_local: float
    status_code: int
