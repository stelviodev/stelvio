"""Explicit developer images never change or weaken the pinned release selection."""

import hashlib
import os
import struct
from types import SimpleNamespace
from unittest.mock import Mock

from pytest import mark, raises

from stelvio.tunnel import assets


def _image(path):
    path.write_bytes(struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0))
    path.chmod(0o755)
    return path


def _allow_no_acl(monkeypatch):
    monkeypatch.setattr(assets, "_no_acl", lambda *args, **kwargs: None)


def _report_version(monkeypatch, output=None):
    monkeypatch.setattr(
        assets.subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=assets.EXPECTED_VERSION.encode() if output is None else output,
                stderr=b"",
            )
        ),
    )


def test_default_selection_is_the_release_pin_without_cache_or_network(monkeypatch):
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    ensure = Mock(side_effect=AssertionError("release cache accessed"))
    download = Mock(side_effect=AssertionError("network accessed"))
    monkeypatch.setattr(assets, "ensure_traforo", ensure)
    monkeypatch.setattr(assets.requests, "get", download)

    selected = assets.select_traforo({})

    assert selected.digest == assets.TARGETS["arm64"]["sha256"]
    assert selected.local_path is None
    assert selected.notice is None
    ensure.assert_not_called()
    download.assert_not_called()


@mark.parametrize(
    ("acl_entry", "unsafe"),
    [
        (b" 0: UUID deny delete\n", False),
        (b" 0: UUID allow read,readattr,list,search\n", False),
        (b" 0: UUID allow add_file\n", True),
        (b" 0: UUID allow write\n", True),
        (b" 0: UUID allow unknown_right\n", True),
        (b" 0: UUID unknown read\n", True),
        (b" malformed\n", True),
        (b" 0: UUID deny delete\n 1: UUID allow write\n", True),
    ],
)
@mark.parametrize("marker", [b"+", b"@"])
def test_acl_validation_rejects_write_grants_but_allows_restrictive_entries(
    tmp_path, monkeypatch, acl_entry, unsafe, marker
):
    result = Mock()
    result.returncode = 0
    result.communicate.return_value = (
        b"drwxr-x---" + marker + b" 1 developer staff 32 local-cache\n" + acl_entry,
        b"",
    )
    monkeypatch.setattr(assets.subprocess, "Popen", Mock(return_value=result))

    if unsafe:
        with raises(assets.NativeAssetError, match="ACLs"):
            assets._no_acl(tmp_path, "cache")
    else:
        assets._no_acl(tmp_path, "cache")


@mark.parametrize(
    ("output", "valid"),
    [
        (b"drwxr-x--- 1 developer staff 32 cache\n", True),
        (b"drwxr-x---@ 1 developer staff 32 cache\n", True),
        (b"drwxr-x---+ 1 developer staff 32 cache\n", False),
        (b"drwxr-x--- 1 developer staff 32 cache\n 0: UUID deny delete\n", False),
        (b"", False),
        (b"\n", False),
    ],
)
def test_acl_metadata_marker_and_rows_must_agree(tmp_path, monkeypatch, output, valid):
    process = Mock(returncode=0)
    process.communicate.return_value = (output, b"")
    monkeypatch.setattr(assets.subprocess, "Popen", Mock(return_value=process))
    if valid:
        assets._no_acl(tmp_path, "cache")
    else:
        with raises(assets.NativeAssetError, match="ACLs"):
            assets._no_acl(tmp_path, "cache")


@mark.parametrize("failure", ["start", "exit", "timeout"])
def test_failed_acl_inspection_is_refused(tmp_path, monkeypatch, failure):
    process = Mock(returncode=1 if failure == "exit" else 0)
    process.communicate.return_value = (b"drwx------@ 1 developer staff 32 cache\n", b"")
    start = Mock(return_value=process)
    if failure == "start":
        start.side_effect = OSError("cannot inspect")
    elif failure == "timeout":
        process.communicate.side_effect = [assets.subprocess.TimeoutExpired("ls", 5), (b"", b"")]
    monkeypatch.setattr(assets.subprocess, "Popen", start)

    with raises(assets.NativeAssetError, match="Cannot verify cache ACLs"):
        assets._no_acl(tmp_path, "cache")
    if failure == "timeout":
        process.kill.assert_called_once()
        assert process.communicate.call_count == 2


@mark.parametrize("unsafe", [False, True])
def test_developer_selection_validates_acl_rows_with_extended_attributes(
    tmp_path, monkeypatch, unsafe
):
    image = _image(tmp_path / "developer")
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    _report_version(monkeypatch)
    process = Mock(returncode=0)
    entry = b"allow write" if unsafe else b"deny delete"
    process.communicate.return_value = (
        b"drwx------@ 1 developer staff 32 path\n 0: group:everyone " + entry + b"\n",
        b"",
    )
    monkeypatch.setattr(assets.subprocess, "Popen", Mock(return_value=process))
    download = Mock(side_effect=AssertionError("unexpected release download"))
    monkeypatch.setattr(assets.requests, "get", download)
    if unsafe:
        with raises(assets.NativeAssetError, match="unsafe extended ACLs"):
            assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(image)})
    else:
        selected = assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(image)})
        assert selected.local_path == image
    download.assert_not_called()


def test_explicit_override_is_selected_without_changing_pins_or_environment(tmp_path, monkeypatch):
    image = _image(tmp_path / "local image")
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    _allow_no_acl(monkeypatch)
    _report_version(monkeypatch)
    monkeypatch.setenv(assets.DEVELOPMENT_BINARY_ENV, "parent-value")
    pins = {arch: dict(target) for arch, target in assets.TARGETS.items()}

    selected = assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(image)})

    assert selected.digest == hashlib.sha256(image.read_bytes()).hexdigest()
    assert selected.local_path == image
    assert selected.digest[:12] in selected.notice
    assert pins == assets.TARGETS
    assert os.environ[assets.DEVELOPMENT_BINARY_ENV] == "parent-value"


@mark.parametrize(
    "bad",
    [
        "relative",
        "missing",
        "symlink",
        "hardlink",
        "directory",
        "parent-writable",
        "writable",
        "special",
        "nonexecutable",
        "foreign",
        "cpu",
        "size",
        "version",
        "acl",
    ],
)
def test_invalid_override_fails_without_release_fallback(  # noqa: C901, PLR0912 - independent refusal cases
    tmp_path, monkeypatch, bad
):
    image = _image(tmp_path / "local-image")
    path = image
    if bad == "relative":
        path = image.relative_to(tmp_path)
    elif bad == "missing":
        path = tmp_path / "missing"
    elif bad == "symlink":
        path = tmp_path / "link"
        path.symlink_to(image)
    elif bad == "hardlink":
        os.link(image, tmp_path / "second-link")
    elif bad == "directory":
        path = tmp_path
    elif bad == "parent-writable":
        parent = tmp_path / "shared-directory"
        parent.mkdir()
        parent.chmod(0o777)
        image = _image(parent / "local-image")
        path = image
    elif bad == "writable":
        image.chmod(0o777)
    elif bad == "special":
        image.chmod(0o4755)
    elif bad == "nonexecutable":
        image.chmod(0o644)
    elif bad == "foreign":
        monkeypatch.setattr(assets.os, "geteuid", lambda: image.stat().st_uid + 1)
    elif bad == "cpu":
        monkeypatch.setattr(assets, "MACHO_CPUS", {"arm64": 123})
    elif bad == "size":
        image.write_bytes(b"short")
    elif bad == "version":
        _report_version(monkeypatch, b"incompatible\n")
    elif bad == "acl":
        monkeypatch.setattr(
            assets,
            "_no_acl",
            Mock(side_effect=assets.NativeAssetError("unsafe extended ACLs")),
        )
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    if bad not in {"version", "acl"}:
        _allow_no_acl(monkeypatch)
        _report_version(monkeypatch)
    ensure = Mock(side_effect=AssertionError("release fallback attempted"))
    download = Mock(side_effect=AssertionError("release download attempted"))
    monkeypatch.setattr(assets, "ensure_traforo", ensure)
    monkeypatch.setattr(assets.requests, "get", download)

    with raises(assets.NativeAssetError):
        assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(path)})

    ensure.assert_not_called()
    download.assert_not_called()


def test_image_change_during_version_check_is_refused(tmp_path, monkeypatch):
    image = _image(tmp_path / "local-image")
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    _allow_no_acl(monkeypatch)

    def mutate(*args, **kwargs):
        image.write_bytes(image.read_bytes() + b"changed")
        return SimpleNamespace(returncode=0, stdout=assets.EXPECTED_VERSION.encode(), stderr=b"")

    monkeypatch.setattr(assets.subprocess, "run", mutate)
    with raises(assets.NativeAssetError, match="changed during validation"):
        assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(image)})


def test_permission_change_during_version_check_is_refused(tmp_path, monkeypatch):
    image = _image(tmp_path / "local-image")
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    _allow_no_acl(monkeypatch)

    def mutate(*args, **kwargs):
        image.chmod(0o777)
        return SimpleNamespace(returncode=0, stdout=assets.EXPECTED_VERSION.encode(), stderr=b"")

    monkeypatch.setattr(assets.subprocess, "run", mutate)
    with raises(assets.NativeAssetError, match="permissions"):
        assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(image)})


@mark.parametrize(
    "output",
    [
        b"v0.0.7\n",
        b"v0.0.7-dev+gabcdef123456\n",
        b"v0.0.6\n",
        b"v0.0.7-dev\n",
        b"v0.0.7-dev+gNOTHEX\n",
        b"stelvio-traforo/1 helper=1 carrier=1\n",
    ],
)
def test_override_accepts_only_pinned_or_well_formed_snapshot_versions(
    tmp_path, monkeypatch, output
):
    image = _image(tmp_path / "local-image")
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    _allow_no_acl(monkeypatch)
    _report_version(monkeypatch, output)
    environment = {assets.DEVELOPMENT_BINARY_ENV: str(image)}

    if output in {b"v0.0.7\n", b"v0.0.7-dev+gabcdef123456\n"}:
        selected = assets.select_traforo(environment)
        assert selected.local_path == image
    else:
        with raises(assets.NativeAssetError, match="must report"):
            assets.select_traforo(environment)
