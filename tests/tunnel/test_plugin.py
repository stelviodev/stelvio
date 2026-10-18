"""Private AWS plugin installation verifies bytes before publishing or executing."""

import hashlib
import io
import struct
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from pytest import fixture, mark, raises

from stelvio.tunnel import plugin
from stelvio.tunnel.helper_client import HelperError


@fixture
def plugin_world(tmp_path, monkeypatch):
    binary = struct.pack("<8I", 0xFEEDFACF, plugin.MACHO_CPUS["arm64"], 0, 2, 0, 0, 0, 0)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr(plugin.MEMBER, binary)
    archive = stream.getvalue()
    monkeypatch.setattr(plugin, "_architecture", lambda: "arm64")
    monkeypatch.setattr(plugin, "get_bin_path", lambda: tmp_path)
    monkeypatch.setattr(
        plugin, "TARGETS", {"arm64": ("mac_arm64", hashlib.sha256(archive).hexdigest())}
    )
    monkeypatch.setattr(plugin, "BINARY_DIGESTS", {"arm64": hashlib.sha256(binary).hexdigest()})
    response = MagicMock()
    response.__enter__.return_value = response
    response.iter_content.side_effect = lambda _: iter([archive])
    download = Mock(return_value=response)
    execute = Mock(return_value=SimpleNamespace(returncode=0, stdout=plugin.PLUGIN_VERSION + "\n"))
    monkeypatch.setattr(plugin.requests, "get", download)
    monkeypatch.setattr(plugin.subprocess, "run", execute)
    return SimpleNamespace(binary=binary, response=response, download=download, execute=execute)


def test_private_install_publishes_verified_executable_and_reuses_exact_pin(plugin_world):
    world = plugin_world
    path = plugin.ensure_plugin()
    assert path.read_bytes() == world.binary
    assert path.stat().st_mode & 0o777 == 0o755
    assert plugin.ensure_plugin() == path
    assert plugin.installed_plugin() == path
    world.download.assert_called_once_with(
        f"https://s3.amazonaws.com/session-manager-downloads/plugin/{plugin.PLUGIN_VERSION}/mac_arm64/sessionmanager-bundle.zip",
        stream=True,
        timeout=(10, 60),
    )
    assert world.execute.call_args.args[0] == [str(path), "--version"]
    assert list(path.parent.glob(".ssm-*")) == []


@mark.parametrize("corruption", ["archive", "binary", "version", "architecture"])
def test_corrupt_download_never_publishes_or_leaves_staging(plugin_world, monkeypatch, corruption):
    world = plugin_world
    if corruption == "archive":
        world.response.iter_content.side_effect = lambda _: iter([b"foreign archive"])
    elif corruption == "binary":
        monkeypatch.setattr(plugin, "BINARY_DIGESTS", {"arm64": "0" * 64})
    elif corruption == "version":
        world.execute.return_value.stdout = "another-version\n"
    else:
        monkeypatch.setattr(plugin, "MACHO_CPUS", {"arm64": 123})
    with raises(HelperError, match=r"digest|version|Mach-O"):
        plugin.ensure_plugin()
    path = plugin.plugin_path()
    assert not path.exists()
    assert list(path.parent.glob(".ssm-*")) == []
    if corruption != "version":
        world.execute.assert_not_called()


def test_missing_or_foreign_private_plugin_has_no_path_fallback(plugin_world, tmp_path):
    path = plugin.plugin_path()
    with raises(HelperError, match="missing"):
        plugin.installed_plugin()
    foreign = tmp_path / "elsewhere"
    foreign.write_bytes(plugin_world.binary)
    path.symlink_to(foreign)
    with raises(HelperError, match="unsafe"):
        plugin.ensure_plugin()
    assert path.is_symlink()
    plugin_world.download.assert_not_called()
    plugin_world.execute.assert_not_called()


def test_interrupted_download_is_retryable_without_partial_install(plugin_world):
    def interrupted(_):
        yield b"partial"
        raise plugin.requests.ConnectionError("fixture disconnect")

    plugin_world.response.iter_content.side_effect = interrupted
    with raises(HelperError, match="download failed"):
        plugin.ensure_plugin()
    assert not plugin.plugin_path().exists()
    plugin_world.execute.assert_not_called()


def test_concurrent_installers_publish_once(plugin_world):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as pool:
        paths = list(pool.map(lambda _: plugin.ensure_plugin(), range(3)))
    assert paths == [plugin.plugin_path()] * 3
    assert paths[0].read_bytes() == plugin_world.binary
    plugin_world.download.assert_called_once()


def test_new_plugin_pin_uses_new_private_path_without_replacing_previous_version(
    plugin_world, monkeypatch
):
    first = plugin.ensure_plugin()
    monkeypatch.setattr(plugin, "PLUGIN_VERSION", "test-next-version")
    plugin_world.execute.return_value.stdout = "test-next-version\n"
    second = plugin.ensure_plugin()
    assert second != first
    assert first.read_bytes() == second.read_bytes() == plugin_world.binary
    assert plugin_world.download.call_count == 2


def test_owned_nonexecutable_cache_is_repaired(plugin_world):
    path = plugin.ensure_plugin()
    path.chmod(0o600)
    assert plugin.ensure_plugin() == path
    assert path.stat().st_mode & 0o777 == 0o755
    assert plugin_world.download.call_count == 2
