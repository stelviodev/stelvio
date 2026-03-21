import importlib
import logging
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


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
