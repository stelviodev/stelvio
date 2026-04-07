from pulumi.automation.events import StepEventMetadata
from rich.console import Console

import stelvio.pulumi as pulumi_module


class _FakeDiagnostic:
    def __init__(self, message: str, urn: str = "", severity: str = "error"):
        self.message = message
        self.urn = urn
        self.severity = severity


class _FakeHandler:
    def __init__(
        self,
        diagnostics: list[_FakeDiagnostic],
        context: str | None = None,
        *,
        has_inline_errors: bool = False,
        compact: bool = False,
        is_preview: bool = False,
    ):
        self.error_diagnostics = diagnostics
        self._context = context
        self.resources = {"urn": type("R", (), {"error": "boom"})()} if has_inline_errors else {}
        self.compact = compact
        self.is_preview = is_preview

    def describe_urn(self, urn: str) -> str | None:
        if self._context:
            return self._context
        return None


def test_show_simple_error_includes_resource_context(monkeypatch) -> None:
    console = Console(record=True, width=160)
    monkeypatch.setattr(pulumi_module, "console", console)

    handler = _FakeHandler(
        diagnostics=[
            _FakeDiagnostic(
                message=(
                    "sdk-v2/provider2.go:572: sdk.helper_schema: all attributes must be indexed. "
                    'Unused attributes: ["email"]: provider=aws@7.16.0'
                ),
                urn="urn:pulumi:michal::stelvio-app::stelvio:aws:DynamoTable::users",
            )
        ],
        context="DynamoTable users",
    )

    pulumi_module._show_simple_error(Exception("boom"), handler)  # type: ignore[arg-type]
    output = console.export_text()

    assert "Resource: DynamoTable users" in output
    assert 'Unused attributes: ["email"]' in output
    assert "✕ Failed" in output


def test_show_simple_error_without_resource_context(monkeypatch) -> None:
    console = Console(record=True, width=160)
    monkeypatch.setattr(pulumi_module, "console", console)

    handler = _FakeHandler(
        diagnostics=[
            _FakeDiagnostic(
                message=(
                    'ValidationError: all attributes must be indexed. Unused attributes: ["email"]'
                ),
                urn="urn:pulumi:michal::stelvio-app::stelvio:aws:DynamoTable::users",
            )
        ]
    )

    pulumi_module._show_simple_error(Exception("boom"), handler)  # type: ignore[arg-type]
    output = console.export_text()

    assert "Resource:" not in output
    assert 'Unused attributes: ["email"]' in output


def test_show_simple_error_skips_duplicate_when_inline_error_exists(monkeypatch) -> None:
    console = Console(record=True, width=160)
    monkeypatch.setattr(pulumi_module, "console", console)

    handler = _FakeHandler(
        diagnostics=[
            _FakeDiagnostic(
                message=(
                    'ValidationError: all attributes must be indexed. Unused attributes: ["email"]'
                )
            )
        ],
        has_inline_errors=True,
    )

    pulumi_module._show_simple_error(Exception("boom"), handler)  # type: ignore[arg-type]
    output = console.export_text()

    assert "See failed resource details above." in output


def test_show_simple_error_keeps_details_for_compact_preview(monkeypatch) -> None:
    console = Console(record=True, width=160)
    monkeypatch.setattr(pulumi_module, "console", console)

    handler = _FakeHandler(
        diagnostics=[
            _FakeDiagnostic(
                message=(
                    'ValidationError: all attributes must be indexed. Unused attributes: ["email"]'
                )
            )
        ],
        has_inline_errors=True,
        compact=True,
        is_preview=True,
    )

    pulumi_module._show_simple_error(Exception("boom"), handler)  # type: ignore[arg-type]
    output = console.export_text()

    assert "See failed resource details above." not in output
    assert 'Unused attributes: ["email"]' in output


def test_step_event_metadata_from_json_handles_null_detailed_diff() -> None:
    payload = {
        "op": "same",
        "urn": "urn:pulumi:test::test::pulumi:pulumi:Stack::test",
        "type": "pulumi:pulumi:Stack",
        "provider": "",
        "detailedDiff": None,
    }

    metadata = StepEventMetadata.from_json(payload)

    assert metadata.detailed_diff == {}


def test_step_event_metadata_from_json_preserves_non_null_detailed_diff() -> None:
    payload = {
        "op": "update",
        "urn": "urn:pulumi:test::test::aws:lambda/function:Function::fn",
        "type": "aws:lambda/function:Function",
        "provider": "",
        "detailedDiff": {
            "memorySize": {
                "diffKind": "update",
                "inputDiff": False,
            }
        },
    }

    metadata = StepEventMetadata.from_json(payload)

    assert metadata.detailed_diff is not None
    assert "memorySize" in metadata.detailed_diff
