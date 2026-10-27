"""Acceptance checks for explicitly supplied Python distribution artifacts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from stelvio.tunnel.assets import TARGETS, TRAFORO_VERSION


@pytest.mark.parametrize("kind", ["WHEEL", "SDIST"])
def test_python_archives_exclude_traforo_build_inputs_and_binaries(kind):
    value = os.environ.get(f"STLV_TEST_STELVIO_{kind}")
    if not value:
        pytest.skip(f"Archive acceptance pending: supply STLV_TEST_STELVIO_{kind}")
    path = Path(value)
    assert path.is_absolute()
    assert path.is_file()
    if kind == "WHEEL":
        with zipfile.ZipFile(path) as archive:
            files = [(name, archive.read(name)) for name in archive.namelist()]
    else:
        with tarfile.open(path) as archive:
            files = [
                (item.name, archive.extractfile(item).read())
                for item in archive.getmembers()
                if item.isfile()
            ]

    assert files
    forbidden_magic = (
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xce",
        b"\xcf\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
        b"\xca\xfe\xba\xbf",
        b"\xbf\xba\xfe\xca",
        b"\x7fELF",
        b"MZ",
        b"!<arch>\n",
    )
    for name, data in files:
        path_parts = PurePosixPath(name).parts
        assert "traforo-client-go" not in path_parts, name
        assert "_assets" not in path_parts, name
        assert not name.endswith((".go", ".c", ".h")), name
        assert not ("scripts" in path_parts and path_parts[-1] == "build.py"), name
        assert not PurePosixPath(name).name.startswith(
            ("traforo-darwin-", "stelvio-traforo-macos-", "session-manager-plugin")
        ), name
        assert not data.startswith(forbidden_magic), name


@pytest.fixture
def candidate_wheel():
    value = os.environ.get("STLV_TEST_STELVIO_WHEEL")
    if not value:
        pytest.skip("Installed-wheel acceptance pending: supply STLV_TEST_STELVIO_WHEEL")
    path = Path(value)
    assert path.is_absolute()
    assert path.is_file()
    return path


def test_isolated_wheel_import_and_cli(candidate_wheel, tmp_path):
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "STLV_TRAFORO_DEV_BINARY"):
        environment.pop(key, None)
    home = tmp_path / "home"
    home.mkdir()
    environment["HOME"] = str(home)

    def run(*arguments, check=True):
        return subprocess.run(  # noqa: S603 - explicit candidate wheel and isolated commands
            arguments,
            cwd=tmp_path,
            env=environment,
            check=check,
            capture_output=True,
            text=True,
            timeout=240,
        )

    virtualenv = tmp_path / "wheel-venv"
    run(sys.executable, "-m", "venv", str(virtualenv))
    python = str(virtualenv / "bin/python")
    run("uv", "pip", "install", "--python", python, str(candidate_wheel))
    run(
        python,
        "-I",
        "-c",
        """import json,sys
from pathlib import Path
import stelvio
from stelvio.tunnel import assets
assert Path(stelvio.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert assets.TRAFORO_VERSION == sys.argv[1]
assert assets.TARGETS == json.loads(sys.argv[2])
""",
        TRAFORO_VERSION,
        json.dumps(TARGETS, sort_keys=True),
    )
    help_result = run(
        python,
        "-I",
        "-c",
        "from stelvio.cli import main; main()",
        "tunnel",
        "--help",
    )
    command_names = {
        line.split()[0]
        for line in help_result.stdout.split("Commands:\n", 1)[-1].splitlines()
        if line.strip()
    }
    assert command_names == {
        "cleanup",
        "status",
        "install",
        "start",
        "trust",
        "uninstall",
        "diagnose",
    }
    status_help = run(
        python,
        "-I",
        "-c",
        "from stelvio.cli import main; main()",
        "tunnel",
        "status",
        "--help",
    )
    assert "Usage:" in status_help.stdout
    inspect_result = run(
        python,
        "-I",
        "-c",
        "from stelvio.cli import main; main()",
        "tunnel",
        "inspect",
        check=False,
    )
    assert inspect_result.returncode == 2
    assert "No such command 'inspect'" in inspect_result.stderr
    trust_help = run(
        python,
        "-I",
        "-c",
        "from stelvio.cli import main; main()",
        "tunnel",
        "trust",
        "--help",
    )
    assert "Commands:" in trust_help.stdout
    reset_help = run(
        python,
        "-I",
        "-c",
        "from stelvio.cli import main; main()",
        "tunnel",
        "trust",
        "reset",
        "--help",
    )
    assert {"--partition", "--account", "--region", "--instance-id"} <= set(
        reset_help.stdout.split()
    )
    start_help = run(
        python,
        "-I",
        "-c",
        "from stelvio.cli import main; main()",
        "tunnel",
        "start",
        "--help",
    )
    assert "start [OPTIONS] [ENV]" in start_help.stdout

    diagnostic_result = run(
        python,
        "-I",
        "-c",
        "from stelvio.cli import main; main()",
        "tunnel",
        "diagnose",
        "--json",
    )
    diagnostic_report = json.loads(diagnostic_result.stdout)
    assert diagnostic_report["schema_version"] == 2
    assert {"versions", "artifacts", "helper", "sessions"} <= set(diagnostic_report["sections"])
    assert '"output":' not in json.dumps(diagnostic_report)
    table_result = run(
        python,
        "-I",
        "-c",
        "from stelvio.cli import main; main()",
        "tunnel",
        "diagnose",
    )
    assert "Stelvio tunnel diagnostics" in table_result.stdout
    assert "'data':" not in table_result.stdout
    run(
        python,
        "-I",
        "-c",
        "from pathlib import Path; "
        "from stelvio.tunnel.diagnostics import protocol, runtime, report, parsers, rendering; "
        "from stelvio.pulumi import get_stelvio_config_dir; "
        "assert Path(runtime.__file__).with_name('README.md').is_file(); "
        "assert not (get_stelvio_config_dir() / 'bin').exists()",
    )
