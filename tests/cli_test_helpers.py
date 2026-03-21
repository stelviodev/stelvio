import importlib
import logging
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from pulumi.automation import OutputValue


def import_cli_module() -> ModuleType:
    with (
        patch("platformdirs.user_log_dir", return_value=str(Path.cwd() / ".tmp-test-logs")),
        patch("logging.handlers.TimedRotatingFileHandler"),
    ):
        module = importlib.import_module("stelvio.cli")

    logging.getLogger("stelvio").handlers = [
        handler
        for handler in logging.getLogger("stelvio").handlers
        if isinstance(getattr(handler, "level", None), int)
    ]
    return module


def import_cli_commands_module() -> ModuleType:
    import_cli_module()
    return importlib.import_module("stelvio.cli.commands")


class FakeStack:
    def __init__(self, outputs: dict[str, OutputValue] | None = None):
        self._outputs = outputs or {}

    def outputs(self) -> dict[str, OutputValue]:
        return self._outputs

    def up(self, on_event) -> None:
        return None

    def preview(self, on_event) -> None:
        return None

    def refresh(self, on_event) -> None:
        return None

    def destroy(self, on_event) -> None:
        return None

    def export_stack(self) -> SimpleNamespace:
        return SimpleNamespace(deployment={"resources": [{"type": "pulumi:pulumi:Stack"}]})


class FakeCommandRun:
    def __init__(
        self,
        state: dict,
        *,
        app_name: str = "demo",
        outputs: dict[str, OutputValue] | None = None,
        has_deployed: bool = True,
    ) -> None:
        self.app_name = app_name
        self.has_deployed = has_deployed
        self.stack = FakeStack(outputs)
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def load_state(self) -> dict:
        return self._state

    def start_partial_push(self) -> None:
        return None

    def stop_partial_push(self) -> None:
        return None

    def push_state(self) -> None:
        return None

    def create_state_snapshot(self) -> None:
        return None

    def delete_snapshots(self) -> None:
        return None

    def complete_update(self, errors=None) -> None:
        return None

    def event_handler(self, display):
        return Mock()
