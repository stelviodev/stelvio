import stelvio.pulumi_compat as compat


def _make_buggy_step_event_metadata_class() -> type:
    class _BuggyStepEventMetadata:
        @classmethod
        def from_json(cls, data: dict) -> dict:
            return dict(data.get("detailedDiff", {}).items())

    return _BuggyStepEventMetadata


def _make_fixed_step_event_metadata_class() -> type:
    class _FixedStepEventMetadata:
        @classmethod
        def from_json(cls, data: dict) -> dict:
            detailed_diff = data.get("detailedDiff") or {}
            return dict(detailed_diff.items())

    return _FixedStepEventMetadata


def _make_unexpected_step_event_metadata_class() -> type:
    class _UnexpectedStepEventMetadata:
        @classmethod
        def from_json(cls, data: dict) -> dict:
            raise RuntimeError("unexpected parser failure")

    return _UnexpectedStepEventMetadata


def test_apply_pulumi_compat_patch_handles_null_detailed_diff(monkeypatch) -> None:
    buggy = _make_buggy_step_event_metadata_class()
    monkeypatch.setattr(compat, "StepEventMetadata", buggy)

    applied = compat.apply_pulumi_automation_compatibility_fixes()

    assert applied is True
    assert buggy.from_json({"detailedDiff": None}) == {}
    assert buggy.from_json({"detailedDiff": {"memorySize": "update"}}) == {"memorySize": "update"}


def test_apply_pulumi_compat_patch_is_idempotent(monkeypatch) -> None:
    buggy = _make_buggy_step_event_metadata_class()
    monkeypatch.setattr(compat, "StepEventMetadata", buggy)

    first = compat.apply_pulumi_automation_compatibility_fixes()
    second = compat.apply_pulumi_automation_compatibility_fixes()

    assert first is True
    assert second is False


def test_apply_pulumi_compat_patch_skips_when_not_needed(monkeypatch) -> None:
    fixed = _make_fixed_step_event_metadata_class()
    monkeypatch.setattr(compat, "StepEventMetadata", fixed)

    applied = compat.apply_pulumi_automation_compatibility_fixes()

    assert applied is False
    assert fixed.from_json({"detailedDiff": None}) == {}


def test_apply_pulumi_compat_patch_skips_on_unexpected_probe_exception(monkeypatch) -> None:
    unexpected = _make_unexpected_step_event_metadata_class()
    monkeypatch.setattr(compat, "StepEventMetadata", unexpected)

    applied = compat.apply_pulumi_automation_compatibility_fixes()

    assert applied is False
