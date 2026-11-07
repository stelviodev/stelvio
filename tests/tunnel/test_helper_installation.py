"""Installation admission through the public commands; live ownership has a Mac proof."""

import shlex
import subprocess
from contextlib import contextmanager
from hashlib import sha256
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from stelvio.cli.tunnel import tunnel
from stelvio.tunnel import installation
from stelvio.tunnel.helper_client import (
    HelperBusyError,
    HelperError,
    HelperOwnershipState,
    HelperUnitState,
    HelperUnitStatus,
)
from stelvio.tunnel.plugin import PluginAssetError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(installation, "ensure_plugin", lambda: None)
    asset = tmp_path / "asset with 'quotes' $(touch SHOULD_NOT_EXIST)"
    asset.write_bytes(b"native-test-asset")
    monkeypatch.setattr(
        installation,
        "select_traforo",
        lambda environment=None: installation.TraforoSelection(
            sha256(asset.read_bytes()).hexdigest()
        ),
    )
    installed = tmp_path / "installed"
    monkeypatch.setattr(installation, "INSTALLED_HELPER", installed)
    monkeypatch.setattr(installation, "_supported", lambda **kwargs: None)
    monkeypatch.setattr(installation, "_installed_digest", lambda **kwargs: None)

    @contextmanager
    def packaged():
        yield asset

    monkeypatch.setattr(installation, "downloaded_helper", packaged)
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


def test_administration_lock_is_a_persistent_validated_regular_file():
    script = installation._authorization_script("echo 'fixed bootstrap'")

    assert f"lock={installation.ADMIN_LOCK}" in script
    assert '(umask 077; set -C; : > "$lock")' in script
    assert '[ ! -L "$lock" ] && [ -f "$lock" ]' in script
    assert 'stat -f %u "$lock"' in script
    assert 'stat -f %Lp "$lock"' in script
    assert 'stat -f %l "$lock"' in script
    assert 'lockf -k -n -t 0 "$lock"' in script
    assert 'lockf -k -n -t 0 "$parent"' not in script
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
        lambda: SimpleNamespace(status=lambda: HelperOwnershipState(False, False, False)),
    )

    def forbidden():
        raise AssertionError("Matching installed helper attempted a cache/download")

    monkeypatch.setattr(installation, "downloaded_helper", forbidden)
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 0, result.output
    assert calls == []


def test_changed_install_uses_current_package_upgrade(setup, monkeypatch):
    _, _, calls = setup
    monkeypatch.setattr(installation, "_installed_digest", lambda: "0" * 64)
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 0
    assert len(calls) == 1
    assert "helper uninstall" in calls[0]


def test_uncertain_host_state_requires_recovery_before_install(setup, monkeypatch):
    asset, installed, calls = setup
    installed.write_bytes(asset.read_bytes())
    monkeypatch.setattr(
        installation, "_installed_digest", lambda: sha256(asset.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(
        installation,
        "NativeHelper",
        lambda: SimpleNamespace(status=lambda: HelperOwnershipState(False, True, False)),
    )
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 1
    assert "stlv tunnel cleanup" in result.output
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
    result = CliRunner().invoke(tunnel, ["uninstall"])
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

    monkeypatch.setattr(installation, "downloaded_helper", forbidden)
    monkeypatch.setattr(installation, "_authorize", forbidden)
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 1
    assert "macOS 15+" in result.output


def test_installed_image_validation_refuses_unsafe_modes_and_symlink_paths(tmp_path, monkeypatch):
    image = tmp_path / "unsafe-mode-image"
    image.write_bytes(b"not a trusted installed image")
    image.chmod(0o777)
    monkeypatch.setattr(installation, "INSTALLED_HELPER", image)
    with pytest.raises(HelperError):
        installation._installed_digest()

    link = tmp_path / "installed-link"
    link.symlink_to(image)
    monkeypatch.setattr(installation, "INSTALLED_HELPER", link)
    with pytest.raises(HelperError, match="unsafe"):
        installation._installed_digest()


def test_plugin_install_failure_stops_before_helper_authorization_or_mutation(setup, monkeypatch):
    _, installed, calls = setup
    installed.write_bytes(b"existing verified helper")
    previous_helper = installed.read_bytes()

    def fail_plugin():
        raise PluginAssetError("AWS plugin checksum does not match its pin")

    monkeypatch.setattr(installation, "ensure_plugin", fail_plugin)
    monkeypatch.setattr(
        installation,
        "_installed_digest",
        lambda: pytest.fail("helper state was queried after plugin installation failed"),
    )

    result = CliRunner().invoke(tunnel, ["install"])

    assert result.exit_code == 1
    assert "AWS plugin checksum does not match its pin" in result.output
    assert "Traceback" not in result.output
    assert calls == []
    assert installed.read_bytes() == previous_helper


def test_matching_active_install_is_idempotent_without_elevation(setup, monkeypatch):
    asset, installed, calls = setup
    installed.write_bytes(asset.read_bytes())
    monkeypatch.setattr(
        installation, "_installed_digest", lambda: sha256(asset.read_bytes()).hexdigest()
    )

    def busy():
        raise HelperBusyError("Native helper request failed: busy")

    monkeypatch.setattr(installation, "NativeHelper", lambda: SimpleNamespace(status=busy))

    def forbidden():
        raise AssertionError("Matching installed helper attempted a cache/download")

    monkeypatch.setattr(installation, "downloaded_helper", forbidden)
    result = CliRunner().invoke(tunnel, ["install"])
    assert result.exit_code == 0, result.output
    assert calls == []


def test_uninstall_uses_installed_image_without_cache_or_download(setup, monkeypatch):
    _, installed, calls = setup
    monkeypatch.setattr(installation, "_installed_digest", lambda: "a" * 64)

    def forbidden():
        raise AssertionError("Uninstall attempted download")

    monkeypatch.setattr(installation, "downloaded_helper", forbidden)
    result = CliRunner().invoke(tunnel, ["uninstall"])
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert f"{installed} helper uninstall" in calls[0]


@pytest.mark.parametrize("developer", [False, True])
def test_forwarder_validates_installed_pin_without_cache(monkeypatch, developer):
    checked = []
    monkeypatch.setattr(
        "stelvio.tunnel.assets.validate_version", lambda path, **kw: checked.append((path, kw))
    )
    monkeypatch.setattr(
        "stelvio.tunnel.assets.ensure_traforo",
        lambda: (_ for _ in ()).throw(AssertionError("runtime downloaded an asset")),
    )
    from stelvio.tunnel.forwarding import packaged_forwarder

    monkeypatch.setattr(installation, "_installed_digest", lambda: "a" * 64)
    monkeypatch.setattr(
        "stelvio.tunnel.assets.select_traforo",
        lambda: installation.TraforoSelection(
            "a" * 64, Path("/local/traforo") if developer else None
        ),
    )
    with packaged_forwarder() as path:
        assert path == installation.INSTALLED_HELPER
    assert checked == [(installation.INSTALLED_HELPER, {"allow_development": developer})]
    monkeypatch.setattr(installation, "_installed_digest", lambda: "b" * 64)
    with pytest.raises(RuntimeError, match="differs"), packaged_forwarder():
        raise AssertionError("Wrong forwarder yielded")


def test_tunnel_help_lists_status_without_inspect_alias():
    result = CliRunner().invoke(tunnel, ["--help"])
    assert result.exit_code == 0
    commands = result.output.split("Commands:\n", 1)[1]
    assert {line.split()[0] for line in commands.splitlines() if line.strip()} == {
        "install",
        "status",
        "cleanup",
        "uninstall",
    }
    assert "Show the helper's owned networking state." in result.output
    assert "inspect" not in result.output


def test_status_and_cleanup_do_not_require_plugin_or_release_download(monkeypatch):
    module = import_module("stelvio.cli.tunnel")
    calls = []

    class OfflineHelper:
        def status(self):
            calls.append("status")
            return HelperOwnershipState(
                True,
                False,
                True,
                (HelperUnitState("12345678", 42, HelperUnitStatus.ACTIVE),),
            )

        def reconcile(self):
            calls.append("cleanup")

    def forbidden():
        raise AssertionError("offline administration attempted a plugin download")

    monkeypatch.setattr(module, "NativeHelper", OfflineHelper)
    monkeypatch.setattr(installation, "ensure_plugin", forbidden)
    monkeypatch.setattr(installation, "downloaded_helper", forbidden)

    status_result = CliRunner().invoke(tunnel, ["status"])
    cleanup_result = CliRunner().invoke(tunnel, ["cleanup"])

    assert status_result.exit_code == 0, status_result.output
    assert "owned=True uncertain=False mutating=True" in status_result.output
    assert "12345678 generation=42 active" in status_result.output
    assert cleanup_result.exit_code == 0, cleanup_result.output
    assert calls == ["status", "cleanup"]


def test_inspect_is_rejected_and_unavailable_helper_error_is_preserved(monkeypatch):
    module = import_module("stelvio.cli.tunnel")

    class UnavailableHelper:
        def status(self):
            raise HelperError("The helper socket is unavailable")

    monkeypatch.setattr(module, "NativeHelper", UnavailableHelper)
    unknown = CliRunner().invoke(tunnel, ["inspect"])
    unavailable = CliRunner().invoke(tunnel, ["status"])

    assert unknown.exit_code == 2
    assert "No such command 'inspect'" in unknown.output
    assert unavailable.exit_code == 1
    assert "The helper socket is unavailable" in unavailable.output
    assert "Traceback" not in unavailable.output


def test_root_cli_registers_only_step_eight_tunnel_commands():
    from stelvio.cli import cli

    result = CliRunner().invoke(cli, ["tunnel", "--help"])

    assert result.exit_code == 0, result.output
    assert "install" in result.output
    assert "status" in result.output
    assert "inspect" not in result.output
    assert "cleanup" in result.output
    assert "uninstall" in result.output
    assert "start" not in result.output
