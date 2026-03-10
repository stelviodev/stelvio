"""Compatibility shims for Pulumi SDK parser edge cases.

Temporary workaround for Pulumi Python Automation API regression:
- Issue: https://github.com/pulumi/pulumi/issues/22105
- Fix PR: https://github.com/pulumi/pulumi/pull/22140

Remove this once Stelvio requires a Pulumi release that includes PR #22140.
"""

from __future__ import annotations

import logging
from typing import Any

from pulumi.automation.events import StepEventMetadata

logger = logging.getLogger(__name__)

_PATCH_MARKER = "_stelvio_detailed_diff_null_patch"


def _has_detailed_diff_null_bug() -> bool:
    """Return True when installed Pulumi crashes on detailedDiff=null."""
    payload = {
        "op": "same",
        "urn": "urn:pulumi:test::test::pulumi:pulumi:Stack::test",
        "type": "pulumi:pulumi:Stack",
        "provider": "",
        "detailedDiff": None,
    }
    try:
        StepEventMetadata.from_json(payload)
    except AttributeError as exc:
        return "'NoneType' object has no attribute 'items'" in str(exc)
    except Exception:
        # Defensive: this probe runs during CLI startup. If Pulumi parser behavior
        # changes in pinned/future versions, never let the probe crash the CLI.
        logger.debug(
            "Pulumi detailedDiff=null probe raised unexpected exception",
            exc_info=True,
        )
        return False
    return False


def apply_pulumi_automation_compatibility_fixes() -> bool:
    """Patch Pulumi Automation event parser if null detailedDiff bug is present."""
    if not _has_detailed_diff_null_bug():
        return False

    from_json_func = StepEventMetadata.from_json.__func__
    if getattr(from_json_func, _PATCH_MARKER, False):
        return False

    def _from_json_nullsafe(
        cls: type[StepEventMetadata], data: dict[str, Any]
    ) -> StepEventMetadata:
        if data.get("detailedDiff") is None:
            data = dict(data)
            data["detailedDiff"] = {}
        return from_json_func(cls, data)

    setattr(_from_json_nullsafe, _PATCH_MARKER, True)
    StepEventMetadata.from_json = classmethod(_from_json_nullsafe)
    logger.debug("Applied Pulumi compatibility patch for detailedDiff=null parser regression")
    return True
