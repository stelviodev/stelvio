import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

from pytest import mark

import stelvio.aws.types


@mark.parametrize(
    ("version", "expected"),
    [
        ((3, 10), "python3.10"),
        ((3, 11), "python3.11"),
        ((3, 12), "python3.12"),
        ((3, 13), "python3.13"),
        ((3, 14), "python3.14"),
        ((3, 15), "python3.15"),
        ((3, 9), "python3.14"),
        ((3, 16), "python3.14"),
        ((4, 0), "python3.14"),
    ],
)
def test_default_runtime_matches_interpreter(version, expected, monkeypatch):
    # Import-time selection needs a fresh module namespace for each interpreter;
    # reloading the shared module would leave consumers holding stale imports.
    source = Path(stelvio.aws.types.__file__)
    with monkeypatch.context() as patch:
        patch.setattr(sys, "version_info", SimpleNamespace(major=version[0], minor=version[1]))
        namespace = runpy.run_path(str(source))

    assert namespace["DEFAULT_RUNTIME"] == expected
