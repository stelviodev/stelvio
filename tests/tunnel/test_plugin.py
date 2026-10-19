"""Private AWS plugin installation verifies bytes before publishing or executing."""

import hashlib
import io
import platform
import struct
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from pytest import fixture, mark, raises

from stelvio.tunnel import assets, plugin
from stelvio.tunnel.assets import _no_acl as inspect_acl


@fixture
def plugin_world(tmp_path, monkeypatch):
    binary = struct.pack("<8I", 0xFEEDFACF, plugin.MACHO_CPUS["arm64"], 0, 2, 0, 0, 0, 0)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as bundle:
        bundle.writestr(plugin.MEMBER, binary)
    archive = stream.getvalue()
    monkeypatch.setattr(plugin, "_architecture", lambda: "arm64")
    monkeypatch.setattr(plugin, "get_bin_path", lambda: tmp_path)
    if platform.system() != "Darwin":
        monkeypatch.setattr(plugin, "_no_acl", lambda *args, **kwargs: None)
        # The cache directory validator is shared with the Traforo asset module.
        # Its ACL inspector invokes macOS `ls -lde`, so stub it on non-Darwin CI.
        monkeypatch.setattr(assets, "_no_acl", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        plugin, "TARGETS", {"arm64": ("mac_arm64", hashlib.sha256(archive).hexdigest())}
    )
    monkeypatch.setattr(plugin, "BINARY_DIGESTS", {"arm64": hashlib.sha256(binary).hexdigest()})
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = 200
    response.iter_content.side_effect = lambda _: iter([archive])
    download = Mock(return_value=response)
    execute = Mock(
        return_value=SimpleNamespace(returncode=0, stdout=plugin.PLUGIN_VERSION + "\n", stderr="")
    )
    monkeypatch.setattr(plugin.requests, "get", download)
    monkeypatch.setattr(plugin.subprocess, "run", execute)
    return SimpleNamespace(binary=binary, response=response, download=download, execute=execute)


@mark.parametrize(
    ("architecture", "target", "archive_digest", "binary_digest"),
    [
        (
            "arm64",
            "mac_arm64",
            "7287f20d41eef91643b72dcf28b5f135de49090dd297216a408dc18729e70272",
            "cca46503c725072d76cdeb67ec70d4147c5e570a77fbebe6de38c2c8e8463993",
        ),
        (
            "x86_64",
            "mac",
            "03be79726bbf30ad2a26177d6531636ab6a42f574e0e3326dfb146585cedf260",
            "77dbb32192f0332c293f56123044720d612a1c8e62bf88ad9e58b90fb8afe867",
        ),
    ],
)
def test_plugin_pin_manifest(architecture, target, archive_digest, binary_digest):
    assert plugin.PLUGIN_VERSION == "1.2.835.0"
    assert plugin.TARGETS[architecture] == (target, archive_digest)
    assert plugin.BINARY_DIGESTS[architecture] == binary_digest


def test_private_install_publishes_verified_executable_and_reuses_exact_pin(plugin_world):
    world = plugin_world
    path = plugin.ensure_plugin()
    assert path.read_bytes() == world.binary
    assert path.stat().st_mode & 0o777 == 0o755
    assert plugin.ensure_plugin() == path
    assert plugin.installed_plugin() == path
    world.download.assert_called_once_with(
        f"https://s3.amazonaws.com/session-manager-downloads/plugin/{plugin.PLUGIN_VERSION}/mac_arm64/sessionmanager-bundle.zip",
        auth=plugin._environment_auth,
        stream=True,
        timeout=(10, 60),
        allow_redirects=False,
    )
    assert world.execute.call_args.args[0] == [str(path), "--version"]
    assert list(path.parent.glob(".ssm-*")) == []


@mark.parametrize("unsafe", [False, True])
def test_plugin_cache_validates_acl_rows_with_extended_attributes(
    plugin_world, monkeypatch, unsafe
):
    # Keep the real shared parser on Linux too; only Darwin ls output is mocked.
    monkeypatch.setattr(assets, "_no_acl", inspect_acl)
    monkeypatch.setattr(plugin, "_no_acl", inspect_acl)
    process = Mock(returncode=0)
    entry = b"allow write" if unsafe else b"deny delete"
    process.communicate.return_value = (
        b"drwx------@ 1 developer staff 32 cache\n 0: group:everyone " + entry + b"\n",
        b"",
    )
    monkeypatch.setattr(assets.subprocess, "Popen", Mock(return_value=process))
    if unsafe:
        with raises(plugin.PluginAssetError, match="cache is unavailable or unsafe"):
            plugin.ensure_plugin()
        plugin_world.download.assert_not_called()
        plugin_world.execute.assert_not_called()
    else:
        assert plugin.ensure_plugin().read_bytes() == plugin_world.binary
        plugin_world.download.assert_called_once()


@mark.parametrize("corruption", ["archive", "binary", "version", "architecture", "size"])
def test_corrupt_download_never_publishes_or_leaves_staging(plugin_world, monkeypatch, corruption):
    world = plugin_world
    if corruption == "archive":
        world.response.iter_content.side_effect = lambda _: iter([b"foreign archive"])
    elif corruption == "binary":
        monkeypatch.setattr(plugin, "BINARY_DIGESTS", {"arm64": "0" * 64})
    elif corruption == "version":
        world.execute.return_value.stdout = "another-version\n"
    elif corruption == "architecture":
        monkeypatch.setattr(plugin, "MACHO_CPUS", {"arm64": 123})
    else:
        monkeypatch.setattr(plugin, "MAX_DOWNLOAD", 4)
    with raises(plugin.PluginAssetError, match=r"archive|digest|version|Mach-O|size"):
        plugin.ensure_plugin()
    path = plugin.plugin_path()
    assert not path.exists()
    assert list(path.parent.glob(".ssm-*")) == []
    if corruption != "version":
        world.execute.assert_not_called()


def test_missing_or_foreign_private_plugin_has_no_path_fallback(plugin_world, tmp_path):
    path = plugin.plugin_path()
    with raises(plugin.PluginAssetError, match="missing"):
        plugin.installed_plugin()
    foreign = tmp_path / "elsewhere"
    foreign.write_bytes(plugin_world.binary)
    path.symlink_to(foreign)
    with raises(plugin.PluginAssetError, match="unsafe"):
        plugin.ensure_plugin()
    assert path.is_symlink()
    plugin_world.download.assert_not_called()
    plugin_world.execute.assert_not_called()


def test_interrupted_download_is_retryable_without_partial_install(plugin_world):
    def interrupted(_):
        yield b"partial"
        raise plugin.requests.ConnectionError("fixture disconnect")

    plugin_world.response.iter_content.side_effect = interrupted
    with raises(plugin.PluginAssetError, match="download failed") as error:
        plugin.ensure_plugin()
    assert error.value.__suppress_context__
    assert not plugin.plugin_path().exists()
    plugin_world.execute.assert_not_called()


def test_plugin_redirects_are_https_and_never_receive_netrc_or_authorization(
    plugin_world, monkeypatch
):
    import requests
    from requests import Request, Session

    redirect = MagicMock()
    redirect.__enter__.return_value = redirect
    redirect.status_code = 302
    redirect.headers = {
        "Location": "https://downloads.example/sessionmanager-bundle.zip?signature=private"
    }
    plugin_world.download.side_effect = [redirect, plugin_world.response]

    def forbidden(*args, **kwargs):
        raise AssertionError("Implicit .netrc credentials consulted")

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", forbidden)
    prepared = Session().prepare_request(
        Request(
            "GET",
            "https://s3.amazonaws.com/sessionmanager-bundle.zip",
            auth=plugin._environment_auth,
        )
    )
    assert "Authorization" not in prepared.headers
    assert plugin.ensure_plugin().is_file()
    for call in plugin_world.download.call_args_list:
        assert call.kwargs["auth"] is plugin._environment_auth
        assert "Authorization" not in call.kwargs.get("headers", {})


@mark.parametrize(
    "location", ["http://example.com/plugin.zip", "https://user:pass@example.com/plugin.zip", ""]
)
def test_plugin_rejects_unsafe_redirects(plugin_world, location):
    plugin_world.response.status_code = 302
    plugin_world.response.headers = {"Location": location}
    with raises(plugin.PluginAssetError, match="redirect"):
        plugin.ensure_plugin()
    plugin_world.execute.assert_not_called()
    assert not plugin.plugin_path().exists()


def test_plugin_redirect_loop_is_bounded(plugin_world):
    plugin_world.response.status_code = 302
    plugin_world.response.headers = {
        "Location": "https://downloads.example/sessionmanager-bundle.zip"
    }
    with raises(plugin.PluginAssetError, match="redirect limit"):
        plugin.ensure_plugin()
    assert plugin_world.download.call_count == 6


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
    path.chmod(0o644)
    assert plugin.ensure_plugin() == path
    assert path.stat().st_mode & 0o777 == 0o755
    assert plugin_world.download.call_count == 2


def test_foreign_hardlinked_plugin_cache_is_refused(plugin_world, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(plugin_world.binary)
    plugin.plugin_path().hardlink_to(elsewhere)

    with raises(plugin.PluginAssetError, match="unsafe"):
        plugin.ensure_plugin()

    assert elsewhere.read_bytes() == plugin_world.binary
    plugin_world.download.assert_not_called()
    plugin_world.execute.assert_not_called()


def test_symlinked_plugin_cache_directory_is_refused(plugin_world, tmp_path, monkeypatch):
    real = tmp_path / "real-cache"
    real.mkdir()
    linked = tmp_path / "linked-cache"
    linked.symlink_to(real, target_is_directory=True)
    monkeypatch.setattr(plugin, "get_bin_path", lambda: linked)

    with raises(plugin.PluginAssetError, match="cache"):
        plugin.ensure_plugin()

    plugin_world.download.assert_not_called()


@mark.parametrize("unsafe_lock", ["symlink", "mode"])
def test_unsafe_plugin_lock_is_refused_without_download(plugin_world, tmp_path, unsafe_lock):
    lock = tmp_path / "session-manager-plugin.install.lock"
    if unsafe_lock == "symlink":
        target = tmp_path / "other-lock"
        target.write_text("")
        lock.symlink_to(target)
    else:
        lock.write_text("")
        lock.chmod(0o644)

    with raises(plugin.PluginAssetError, match="lock"):
        plugin.ensure_plugin()

    plugin_world.download.assert_not_called()
