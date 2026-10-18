"""Pinned release downloads are verified before execution and atomic publication."""

import hashlib
import os
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from pytest import fixture, mark, raises

from stelvio.tunnel import assets


@fixture
def world(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    monkeypatch.setattr(assets, "get_bin_path", lambda: tmp_path)
    binary = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0)
    target = dict(assets.TARGETS["arm64"], sha256=hashlib.sha256(binary).hexdigest())
    monkeypatch.setattr(assets, "TARGETS", {"arm64": target})
    response = MagicMock()
    response.__enter__.return_value = response
    response.status_code = 200
    response.iter_content.side_effect = lambda _: iter([binary])
    download = Mock(return_value=response)
    execute = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout=assets.EXPECTED_VERSION.encode(), stderr=b""
        )
    )
    monkeypatch.setattr(assets.requests, "get", download)
    monkeypatch.setattr(assets.subprocess, "run", execute)
    return SimpleNamespace(
        binary=binary, response=response, download=download, execute=execute, target=target
    )


def test_anonymous_download_and_cache_reuse(world):
    path = assets.ensure_traforo()
    assert path.read_bytes() == world.binary
    assert path.stat().st_mode & 0o777 == 0o755
    assert assets.ensure_traforo() == path
    world.download.assert_called_once_with(
        world.target["url"],
        headers={"Accept": "application/octet-stream"},
        auth=assets._environment_auth,
        stream=True,
        timeout=(10, 60),
        allow_redirects=False,
    )
    assert world.execute.call_args.args[0] == [str(path), "version"]
    assert not list(path.parent.glob(".traforo-*"))


def test_authenticated_download_uses_api_and_drops_token_on_cdn_redirect(world, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-token")
    redirect = MagicMock()
    redirect.__enter__.return_value = redirect
    redirect.status_code = 302
    redirect.headers = {
        "Location": "https://release-assets.githubusercontent.com/file?signature=secret"
    }
    world.download.side_effect = [redirect, world.response]
    assets.ensure_traforo()
    first, second = world.download.call_args_list
    assert first.args == (world.target["api_url"],)
    assert first.kwargs["headers"]["Authorization"] == "Bearer test-secret-token"
    assert "Authorization" not in second.kwargs["headers"]


@mark.parametrize("status", [401, 403, 404, 500])
def test_http_error_is_actionable_without_credentials_or_response_body(world, monkeypatch, status):
    monkeypatch.setenv("GITHUB_TOKEN", "test-secret-token")
    world.response.status_code = status
    with raises(assets.NativeAssetError, match=f"HTTP {status}") as error:
        assets.ensure_traforo()
    assert "GITHUB_TOKEN" in str(error.value)
    assert "test-secret-token" not in str(error.value)
    assert world.download.call_count == 1  # Never silently retry unauthenticated.
    assert not assets.traforo_path().exists()
    world.execute.assert_not_called()


@mark.parametrize("corruption", ["digest", "architecture", "size", "version", "stderr"])
def test_invalid_release_never_publishes(world, corruption, monkeypatch):
    if corruption == "digest":
        world.target["sha256"] = "0" * 64
    elif corruption == "architecture":
        monkeypatch.setattr(assets, "MACHO_CPUS", {"arm64": 123})
    elif corruption == "size":
        monkeypatch.setattr(assets, "MAX_BINARY_BYTES", 10)
    elif corruption == "version":
        world.execute.return_value.stdout = b"wrong version\n"
    else:
        world.execute.return_value.stderr = b"unexpected\n"
    with raises(assets.NativeAssetError):
        assets.ensure_traforo()
    assert not assets.traforo_path().exists()
    assert not list(assets.traforo_path().parent.glob(".traforo-*"))
    if corruption not in {"version", "stderr"}:
        world.execute.assert_not_called()


def test_interrupted_stream_hides_signed_url_and_can_retry(world):
    def disconnect(_):
        yield b"partial"
        raise assets.requests.ConnectionError("https://cdn.example/file?secret=token")

    world.response.iter_content.side_effect = disconnect
    with raises(assets.NativeAssetError, match="download failed") as error:
        assets.ensure_traforo()
    assert "secret" not in str(error.value)
    assert error.value.__suppress_context__
    assert not assets.traforo_path().exists()
    world.response.iter_content.side_effect = lambda _: iter([world.binary])
    assert assets.ensure_traforo().read_bytes() == world.binary


@mark.parametrize(
    "location", ["http://example.com/file", "https://user:password@example.com/file", ""]
)
def test_unsafe_redirect_refused(world, location):
    world.response.status_code = 302
    world.response.headers = {"Location": location}
    with raises(assets.NativeAssetError, match="redirect"):
        assets.ensure_traforo()
    world.execute.assert_not_called()


def test_redirect_loop_is_bounded(world):
    world.response.status_code = 302
    world.response.headers = {"Location": world.target["url"]}
    with raises(assets.NativeAssetError, match="redirect limit"):
        assets.ensure_traforo()
    assert world.download.call_count == 6


@mark.parametrize("change", ["corrupt", "nonexecutable"])
def test_owned_cache_repaired(world, change):
    path = assets.ensure_traforo()
    if change == "corrupt":
        path.write_bytes(b"corrupt")
    else:
        path.chmod(0o600)
    assert assets.ensure_traforo() == path
    assert path.read_bytes() == world.binary
    assert path.stat().st_mode & 0o777 == 0o755
    assert world.download.call_count == 2


@mark.parametrize("foreign", ["symlink", "hardlink"])
def test_foreign_cache_refused_without_download(world, tmp_path, foreign):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(world.binary)
    if foreign == "symlink":
        assets.traforo_path().symlink_to(elsewhere)
    else:
        os.link(elsewhere, assets.traforo_path())
    with raises(assets.NativeAssetError, match="unsafe"):
        assets.ensure_traforo()
    assert elsewhere.read_bytes() == world.binary
    world.download.assert_not_called()
    world.execute.assert_not_called()


def test_concurrent_installers_publish_once(world):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3) as pool:
        paths = list(pool.map(lambda _: assets.ensure_traforo(), range(3)))
    assert paths == [assets.traforo_path()] * 3
    assert paths[0].read_bytes() == world.binary
    world.download.assert_called_once()


def test_release_change_uses_separate_cache(world, monkeypatch):
    first = assets.ensure_traforo()
    monkeypatch.setattr(assets, "TRAFORO_VERSION", "0.0.8")
    world.execute.return_value.stdout = b"v0.0.8\n"
    second = assets.ensure_traforo()
    assert first != second
    assert first.read_bytes() == second.read_bytes() == world.binary
    assert world.download.call_count == 2


@mark.parametrize(("machine", "cpu"), [("arm64", 0x0100000C), ("x86_64", 0x01000007)])
def test_architecture_selection_and_validation(monkeypatch, machine, cpu):
    monkeypatch.setattr(assets.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(assets.platform, "machine", lambda: machine)
    assert assets._architecture() == machine
    binary = struct.pack("<8I", 0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0)
    monkeypatch.setitem(assets.TARGETS[machine], "sha256", hashlib.sha256(binary).hexdigest())
    assets._binary(binary, machine)


@mark.parametrize("system", ["Linux", "Windows"])
def test_unsupported_platform_refused_before_io(monkeypatch, system):
    import sys

    monkeypatch.setitem(sys.modules, "fcntl", None)
    monkeypatch.setattr(assets.platform, "system", lambda: system)
    monkeypatch.setattr(assets, "get_bin_path", Mock(side_effect=AssertionError("filesystem")))
    monkeypatch.setattr(assets.requests, "get", Mock(side_effect=AssertionError("network")))
    with raises(assets.NativeAssetError, match="supports macOS"):
        assets.ensure_traforo()


def test_requests_does_not_consult_netrc(world, monkeypatch):
    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("Implicit .netrc credentials consulted")

    monkeypatch.setattr(requests.sessions, "get_netrc_auth", forbidden)
    prepared = requests.Session().prepare_request(
        requests.Request("GET", world.target["url"], auth=assets._environment_auth)
    )
    assert "Authorization" not in prepared.headers
    prepared = requests.Session().prepare_request(
        requests.Request(
            "GET",
            world.target["api_url"],
            headers={"Authorization": "Bearer env-token"},
            auth=assets._environment_auth,
        )
    )
    assert prepared.headers["Authorization"] == "Bearer env-token"


@mark.parametrize("override", [None, ""])
def test_release_version_uses_verified_cache_without_capability_command(
    world, monkeypatch, override
):
    monkeypatch.setenv(assets.DEVELOPMENT_BINARY_ENV, override or "")
    assets.require_network_version()
    assets.require_network_version()
    world.download.assert_called_once()
    assert assets.traforo_path().name == "stelvio-traforo-0.0.7-arm64"
    assert all(call.args[0][-1] == "version" for call in world.execute.call_args_list)


@mark.parametrize(
    "output", [b"invalid", b"{}", b"v0.0.4\n", b"v0.0.6\n", b"v0.0.5\n", b"v0.0.7-dev+g1234567\n"]
)
def test_release_wrong_version_fails_before_networking(world, output):
    path = assets.ensure_traforo()
    world.execute.return_value.stdout = output
    with raises(assets.NativeAssetError, match=r"must report v0\.0\.7"):
        assets.require_network_version({})
    assert path.read_bytes() == world.binary


@mark.parametrize("change", ["removed", "modified"])
def test_release_changed_during_version_check_is_rejected(world, change):
    path = assets.ensure_traforo()

    def execute(arguments, **kwargs):
        if path.exists():
            if change == "removed":
                path.unlink()
            else:
                path.write_bytes(world.binary + b"changed")
        return SimpleNamespace(returncode=0, stdout=assets.EXPECTED_VERSION.encode(), stderr=b"")

    world.execute.side_effect = execute
    with raises(assets.NativeAssetError):
        assets.require_network_version({})


def test_non_semantic_version_output_is_rejected(world):
    path = assets.ensure_traforo()
    world.execute.return_value.stdout = b"stelvio-traforo/1 helper=1 carrier=1\n"
    with raises(assets.NativeAssetError, match="must report"):
        assets.validate_version(path)


@mark.parametrize(
    ("architecture", "suffix", "asset_id", "digest"),
    [
        (
            "arm64",
            "arm64",
            "654424977",
            "14327d6881b2c6d01356fd390ff1c0fd7d57e42fafc2874b5af82a7aafcb2da7",
        ),
        (
            "x86_64",
            "amd64",
            "654424978",
            "a1adc747fc328e36e69ebc2e0e74fbb8a48e6d2eacc9403ae4a22b81bd6757bd",
        ),
    ],
)
def test_v007_release_manifest(architecture, suffix, asset_id, digest):
    assert assets.TRAFORO_VERSION == "0.0.7"
    assert assets.TARGETS[architecture] == {
        "url": f"https://github.com/stelviodev/traforo/releases/download/v0.0.7/traforo-darwin-{suffix}",
        "api_url": f"https://api.github.com/repos/stelviodev/traforo/releases/assets/{asset_id}",
        "sha256": digest,
    }
