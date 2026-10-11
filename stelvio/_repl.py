"""Optional interactive shell using detached local component values."""

import code
import sys
from importlib import import_module


def _start_repl(components: object) -> None:
    import boto3  # noqa: PLC0415  # Loaded only for requested interactive sessions.

    context = {"components": components, "boto3": boto3}
    try:
        try:
            ipython = import_module("IPython")
            ipython.embed(user_ns=context)
        except ModuleNotFoundError as error:
            if error.name != "IPython":
                _report_ipython_failure(error)
        except Exception as error:
            _report_ipython_failure(error)
        else:
            return
        code.interact(banner="", exitmsg="", local={"components": components, "boto3": boto3})
    except SystemExit:
        # A shell exit ends the session successfully, regardless of its supplied status.
        pass


def _report_ipython_failure(error: Exception) -> None:
    sys.stderr.write(f"IPython REPL failed: {type(error).__name__}: {error}\n")
