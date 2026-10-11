import threading
import time
from concurrent.futures import ThreadPoolExecutor

from pulumi.automation.events import StepEventMetadata
from pytest import fixture, mark, param
from rich.console import Console

import stelvio.pulumi as pulumi_module

# Pulumi's diagnostic for an exception in the program: the message, the traceback, then the
# exception line with the message again (a real `stlv diff` on 2026-10-02).
_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "/x/_server.py", line 95, in run\n'
    "    loop.run_until_complete(run_in_stack(self.program))\n"
    '  File "/tmp/probe.py", line 8, in program\n'
    '    raise RuntimeError("...")\n'
)
# The installer's output sits indented under the first line (uv indents its own by two more).
_UV_INSTALL_ERROR = (
    "RuntimeError: [Function: functions/users.handler] Dependency install failed:\n"
    "    × No solution found when resolving dependencies:\n"  # noqa: RUF001  # uv's glyph
    "    ╰─▶ Because nosuchpkg was not found in the package registry and\n"
    "        you require nosuchpkg, we can conclude that your requirements\n"
    "        are unsatisfiable."
)
_PIP_INSTALL_ERROR = (
    "RuntimeError: [Function: functions/users.handler] Dependency install failed:\n"
    "  ERROR: Could not find a version that satisfies the requirement nosuchpkg "
    "(from versions: none)\n"
    "  ERROR: No matching distribution found for nosuchpkg"
)
# pip's own crash: a traceback of its own, which the indent keeps apart from the program's
_PIP_CRASH_ERROR = (
    "RuntimeError: [Function: functions/users.handler] Dependency install failed:\n"
    "  ERROR: Exception:\n"
    "  Traceback (most recent call last):\n"
    '    File "/x/pip/_internal/cli/base_command.py", line 180, in exc_logging_wrapper\n'
    "      status = run_func(*args)\n"
    "  ConnectionError: HTTPSConnectionPool(host='pypi.org', port=443): Max retries exceeded"
)
_ONE_LINE_ERROR = "ValueError: Folder not found: /p/functions/x"
# `raise ... from e`: two tracebacks, the error follows the last one
_CHAINED_TRACEBACK = (
    f"{_TRACEBACK}OSError: [Errno 2] No such file\n\n"
    "The above exception was the direct cause of the following exception:\n\n"
    f"{_TRACEBACK}"
)


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

    def show_completion(self, *, output_lines=None, failed: bool = False) -> None:
        pulumi_module.console.print(f"completion(failed={failed})")


@fixture
def show_simple_error(monkeypatch):
    """Calls `_show_simple_error` with a handler and returns what it printed."""
    console = Console(record=True, width=160)
    monkeypatch.setattr(pulumi_module, "console", console)

    def show(handler: _FakeHandler) -> str:
        pulumi_module._show_simple_error(Exception("boom"), handler)  # type: ignore[arg-type]
        return console.export_text()

    return show


@mark.parametrize(
    ("traceback", "exception_block"),
    [
        param(_TRACEBACK, _UV_INSTALL_ERROR, id="uv_install_error"),
        param(_TRACEBACK, _PIP_INSTALL_ERROR, id="pip_install_error"),
        param(_TRACEBACK, _PIP_CRASH_ERROR, id="pip_crash_with_its_own_traceback"),
        param(_TRACEBACK, _ONE_LINE_ERROR, id="one_line_error"),
        param(_CHAINED_TRACEBACK, _ONE_LINE_ERROR, id="chained_traceback"),
    ],
)
def test_show_simple_error_shows_a_program_exception_whole(
    show_simple_error, traceback, exception_block
) -> None:
    """A multi-line exception message (an installer's output) used to shrink to one line: the
    last line matching `Name: ...`, which for pip was its last `ERROR:` line."""
    message = exception_block.split(": ", 1)[1]
    diagnostic = _FakeDiagnostic(
        message=f"python inline source runtime error: {message}\n{traceback}{exception_block}\n\n"
    )

    output = show_simple_error(_FakeHandler([diagnostic]))

    assert output == f"\n| Error\n\n{exception_block}\n\ncompletion(failed=True)\n"


def test_show_simple_error_includes_resource_context(show_simple_error) -> None:
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

    output = show_simple_error(handler)

    assert output == (
        "\n| Error\n\nResource: DynamoTable users\n"
        "sdk-v2/provider2.go:572: sdk.helper_schema: all attributes must be indexed. "
        'Unused attributes: ["email"]: provider=aws@7.16.0\n\ncompletion(failed=True)\n'
    )


def test_show_simple_error_prints_markup_like_provider_text_verbatim(show_simple_error) -> None:
    handler = _FakeHandler(
        diagnostics=[_FakeDiagnostic(message="Error: invalid name 'api-[dev]' [/x]", urn="u")],
        context="Function api-[dev]",
    )

    # Rich would eat `[dev]` as a style tag and raise MarkupError on the stray `[/x]`
    output = show_simple_error(handler)

    assert output == (
        "\n| Error\n\nResource: Function api-[dev]\n"
        "Error: invalid name 'api-[dev]' [/x]\n\ncompletion(failed=True)\n"
    )


def test_show_simple_error_without_resource_context(show_simple_error) -> None:
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

    output = show_simple_error(handler)

    assert output == (
        "\n| Error\n\nValidationError: all attributes must be indexed. "
        'Unused attributes: ["email"]\n\ncompletion(failed=True)\n'
    )


def test_show_simple_error_skips_duplicate_when_inline_error_exists(show_simple_error) -> None:
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

    output = show_simple_error(handler)

    assert output == "\n| Error\n\nSee failed resource details above.\n\ncompletion(failed=True)\n"


def test_show_simple_error_keeps_details_for_compact_preview(show_simple_error) -> None:
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

    output = show_simple_error(handler)

    assert output == (
        "\n| Error\n\nValidationError: all attributes must be indexed. "
        'Unused attributes: ["email"]\n\ncompletion(failed=True)\n'
    )


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


def test_ensure_pulumi_installs_once_under_concurrent_callers(monkeypatch, tmp_path) -> None:
    """Two `stlv` commands (or xdist workers) on a cold machine must not both install into the
    shared bin/: the second waits on the lock and then finds the CLI already there. Threads
    stand in for processes: flock on two separate open() handles blocks within one process too."""
    installed = threading.Event()
    installs: list[float] = []

    def fake_install() -> None:
        installs.append(time.monotonic())
        time.sleep(0.2)  # the other caller must pass its own pre-lock needs_pulumi() meanwhile
        installed.set()

    monkeypatch.setattr(pulumi_module, "get_bin_path", lambda: tmp_path)
    monkeypatch.setattr(pulumi_module, "needs_pulumi", lambda: not installed.is_set())
    monkeypatch.setattr(pulumi_module, "install_pulumi", fake_install)

    with ThreadPoolExecutor(max_workers=2) as pool:
        callers = [pool.submit(pulumi_module.ensure_pulumi, show_status=False) for _ in range(2)]
    for caller in callers:
        caller.result()  # re-raises a caller's exception instead of leaving a bare `0 == 1`

    assert len(installs) == 1


def test_move_pulumi_to_bin_moves_the_cli_last(monkeypatch, tmp_path) -> None:
    """`needs_pulumi()` only checks `bin/pulumi`, so the CLI must land after its plugins: a
    caller checking mid-install, or after a killed one, must not take a bin/ without
    `pulumi-language-python-exec` for installed."""
    names = ("pulumi", "pulumi-language-python-exec", "pulumi-language-python")

    class FixedOrderDir(type(tmp_path)):  # APFS returns hash order; pin the CLI first
        def iterdir(self):
            return iter(self / name for name in names)

    extracted = FixedOrderDir(tmp_path / "extracted")
    (extracted / "pulumi").mkdir(parents=True)
    for name in names:
        (extracted / "pulumi" / name).write_text("")
    moved: list[str] = []
    monkeypatch.setattr(pulumi_module, "get_bin_path", lambda: tmp_path / "bin")
    monkeypatch.setattr(pulumi_module.shutil, "move", lambda src, _dst: moved.append(src))

    pulumi_module.move_pulumi_to_bin("darwin", extracted)

    assert [name.rsplit("/", 1)[-1] for name in moved] == [*names[1:], "pulumi"]
