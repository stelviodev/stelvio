"""Prebuilt distribution acceptance. Pytest never builds Go or Python artifacts."""

import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

from pytest import fixture, mark, skip


@fixture
def candidate_wheel():
    value = os.environ.get("STLV_TEST_STELVIO_WHEEL")
    if not value:
        skip("Installed-wheel acceptance pending: supply STLV_TEST_STELVIO_WHEEL")
    path = Path(value)
    assert path.is_absolute()
    assert path.is_file()
    return path


@mark.parametrize("kind", ["WHEEL", "SDIST"])
def test_distributions_exclude_native_source_scripts_and_executables(kind):
    value = os.environ.get("STLV_TEST_STELVIO_" + kind)
    if not value:
        skip(f"Archive acceptance pending: supply STLV_TEST_STELVIO_{kind}")
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
    forbidden_magic = (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\x7fELF", b"MZ")
    for name, data in files:
        parts = PurePosixPath(name).parts
        assert "traforo-client-go" not in parts, name
        assert "_assets" not in parts, name
        assert not name.endswith((".go", ".c", ".h")), name
        assert not PurePosixPath(name).name.startswith(
            ("traforo-darwin-", "stelvio-traforo-macos-", "session-manager-plugin")
        ), name
        assert not data.startswith(forbidden_magic), name


def test_isolated_installed_wheel_cli_imports_and_non_vpc(candidate_wheel, tmp_path):
    environment = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME", "STLV_TRAFORO_DEV_BINARY"):
        environment.pop(key, None)

    def run(*args):
        return subprocess.run(  # noqa: S603 - explicit prebuilt candidate and isolated test scripts
            args,
            cwd=tmp_path,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )

    directory = tmp_path / "wheel-venv"
    run(sys.executable, "-m", "venv", str(directory))
    python = str(directory / "bin/python")
    run("uv", "pip", "install", "--python", python, str(candidate_wheel))
    run(
        python,
        "-I",
        "-c",
        """import importlib.util,sys
from pathlib import Path
from unittest.mock import patch
import stelvio
from stelvio.tunnel import assets
from stelvio.tunnel.session import NetworkSession
assert Path(stelvio.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert importlib.util.find_spec('dns') is None
for system in ('Linux', 'Windows'):
    with patch('platform.system', return_value=system):
        with patch.object(assets, 'ensure_traforo', side_effect=AssertionError('activated')):
            session=NetworkSession(state=None,app='plain',env='test',report=print)
            session.start()
            session.close()
        try:
            assets.ensure_traforo()
        except assets.NativeAssetError:
            pass
        else:
            raise AssertionError('unsupported asset selected')
""",
    )
    for command in ("tunnel", "dev"):
        result = run(python, "-I", "-c", "from stelvio.cli import main; main()", command, "--help")
        assert "--network-mode" not in result.stdout
        if command == "tunnel":
            for name in ("install", "start", "inspect", "cleanup", "reconcile", "uninstall"):
                assert name in result.stdout.split()
            assert "recover" not in result.stdout.split()
