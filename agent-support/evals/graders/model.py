from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    family: str  # "functional" | "semantic" | "observation"
    passed: bool
    detail: str = ""


@dataclass
class GradeResult:
    case_id: str
    functional_pass: bool
    semantic_checks: dict[str, bool] = field(default_factory=dict)
    checks: list[CheckResult] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "functional_pass": self.functional_pass,
            "semantic_checks": dict(self.semantic_checks),
            "observations": list(self.observations),
            "error": self.error,
            "checks": [
                {
                    "check_id": c.check_id,
                    "family": c.family,
                    "passed": c.passed,
                    "detail": c.detail,
                }
                for c in self.checks
            ],
        }
