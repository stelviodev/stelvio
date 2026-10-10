"""Installation admission through the public commands; live ownership has a Mac proof."""

import shlex
import subprocess
from contextlib import contextmanager
from hashlib import sha256
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from stelvio.cli.tunnel import tunnel
from stelvio.tunnel import installation
from stelvio.tunnel.helper_client import HelperBusyError, HelperError, HelperInspection


@pytest.fixture
def setup(tmp_path, monkeypatch):
    asset = tmp_path / "asset with 'quotes' $(touch SHOULD_NOT_EXIST)"
    asset.write_bytes(b"native-test-asset")
    installed = tmp_path / "installed"
    monkeypatch.setattr(installation, "INSTALLED_HELPER", installed)
    monkeypatch.setattr(installation, "_supported", lambda **kwargs: None)
    monkeypatch.setattr(installation, "_installed_digest", lambda **kwargs: None)

    @contextmanager
    def packaged():
        yield asset

    monkeypatch.setattr(installation, "packaged_helper", packaged)
    calls = []
    monkeypatch.setattr(installation, "_authorize", calls.append)
    monkeypatch.setattr(installation, "_wait_ready", lambda **kwargs: None)
    return asset, installed, calls


def test_install_displays_system_effects_and_uses_checked_native_bootstrap(setup):
    asset, installed, calls = setup
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 0, result.output
    assert "temporary VPC routes, interfaces, and resolver files" in result.output
    assert "Native VPC helper ready" in result.output
    assert len(calls) == 1
    script = calls[0]
    assert sha256(asset.read_bytes()).hexdigest() in script
    # Fixed root execution, caller asset is only cp input, even with shell syntax.
    assert "env -i PATH=/usr/bin:/bin" in script
    assert f'/bin/cp {shlex.quote(str(asset))} "$stage"' in script
    assert (
        f"/usr/bin/env -i PATH=/usr/bin:/bin {shlex.quote(str(installed))} helper install"
        in script
    )
    assert "python" not in script

    assert (
        subprocess.run(
            ["/bin/sh", "-n"], input=script.encode(), capture_output=True, check=False, timeout=3
        ).returncode
        == 0
    )


def test_healthy_matching_install_is_idempotent_without_elevation(setup, monkeypatch):
    asset, installed, calls = setup
    installed.write_bytes(asset.read_bytes())
    monkeypatch.setattr(
        installation, "_installed_digest", lambda: sha256(asset.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(
        installation,
        "NativeHelper",
        lambda: SimpleNamespace(inspect=lambda: HelperInspection(False, False, False)),
    )
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 0, result.output
    assert calls == []


def test_incompatible_install_refuses_before_authorization(setup, monkeypatch):
    _, _, calls = setup
    monkeypatch.setattr(installation, "_installed_digest", lambda: "0" * 64)
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 1
    assert "matching package" in result.output
    assert calls == []


def test_uncertain_host_state_requires_recovery_before_install(setup, monkeypatch):
    asset, installed, calls = setup
    installed.write_bytes(asset.read_bytes())
    monkeypatch.setattr(
        installation, "_installed_digest", lambda: sha256(asset.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(
        installation,
        "NativeHelper",
        lambda: SimpleNamespace(inspect=lambda: HelperInspection(False, True, False)),
    )
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 1
    assert "stlv tunnel reconcile" in result.output
    assert calls == []


def test_cleanup_passes_active_refusal_through_without_success_message(setup, monkeypatch):
    asset, _, calls = setup
    monkeypatch.setattr(
        installation, "_installed_digest", lambda: sha256(asset.read_bytes()).hexdigest()
    )

    def refuse(script):
        calls.append(script)
        raise HelperError("Helper is active; close its session and retry")

    monkeypatch.setattr(installation, "_authorize", refuse)
    result = CliRunner().invoke(tunnel, ["cleanup"])
    assert result.exit_code == 1
    assert "Helper is active" in result.output
    assert "Native VPC helper removed" not in result.output
    assert len(calls) == 1
    assert "helper uninstall" in calls[0]
    assert "aws" not in calls[0]


def test_platform_refusal_precedes_asset_read_and_authorization(monkeypatch):
    monkeypatch.setattr(installation.platform, "system", lambda: "Linux")

    def forbidden(*args):
        raise AssertionError("Attempted unsupported platform setup")

    monkeypatch.setattr(installation, "packaged_helper", forbidden)
    monkeypatch.setattr(installation, "_authorize", forbidden)
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 1
    assert "macOS 15+" in result.output


def test_matching_active_install_is_idempotent_without_elevation(setup, monkeypatch):
    asset, installed, calls = setup
    installed.write_bytes(asset.read_bytes())
    monkeypatch.setattr(
        installation, "_installed_digest", lambda: sha256(asset.read_bytes()).hexdigest()
    )

    def busy():
        raise HelperBusyError("Native helper request failed: busy")

    monkeypatch.setattr(installation, "NativeHelper", lambda: SimpleNamespace(inspect=busy))
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 0, result.output
    assert calls == []
