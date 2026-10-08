"""A coherent native bundle is required before privileged installation is offered."""

import hashlib
import json
import struct

from pytest import fixture, mark, raises

from stelvio.tunnel import assets
from stelvio.tunnel.native.build import ASSET, LIBRARIES, SOURCES


@fixture
def bundle(tmp_path, monkeypatch):
    native = tmp_path / "native"
    packaged = tmp_path / "_assets"
    native.mkdir()
    packaged.mkdir()
    for name in SOURCES:
        (native / name).write_text(f"/* fixture {name} */\n")
    (native / "io.h").write_text("/* fixture header */\n")
    monkeypatch.setattr(
        assets, "files", lambda package: native if package.endswith(".native") else tmp_path
    )
    # This synthetic header is never executed. Architecture/digest/source
    # coherence are these tests' scope; the real compiled asset has its own proof.
    binary = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0) + b"fixture"
    (packaged / ASSET).write_bytes(binary)
    manifest = {
        "format": 1,
        "helper_abi": 1,
        "carrier_version": 1,
        "asset": ASSET,
        "platform": "macos-arm64-darwin24",
        "sha256": hashlib.sha256(binary).hexdigest(),
        # Independent golden digest pins C/source filename order, NUL framing
        # and exact fixture bytes; never generate it with the reader under test.
        "source_sha256": "15625d8f97a839f432fbff1c965ffd5f1d6e78a558a824ffbd9ce4a435b0ecf0",
        "direct_libraries": sorted(LIBRARIES),
    }
    (packaged / "manifest.json").write_text(json.dumps(manifest))
    return native, packaged, manifest


def test_coherent_bundle_yields_asset_without_executing_it(bundle):
    _, packaged, _ = bundle
    with assets.packaged_helper() as path:
        assert path == packaged / ASSET
        assert path.read_bytes().endswith(b"fixture")


@mark.parametrize(
    ("key", "value", "error"),
    [
        ("format", True, "manifest is incompatible"),
        ("helper_abi", 2, "manifest is incompatible"),
        ("carrier_version", "1", "manifest is incompatible"),
        ("asset", "../../elsewhere", "unsupported artifact"),
        ("platform", "linux-amd64", "unsupported artifact"),
        ("direct_libraries", ["/unexpected/injected.dylib"], "unexpected runtime dependency"),
        ("sha256", "A" * 64, "invalid digest"),
        ("source_sha256", "short", "invalid digest"),
    ],
)
def test_unsupported_manifest_cannot_select_other_paths_or_runtime(bundle, key, value, error):
    _, packaged, manifest = bundle
    manifest[key] = value
    (packaged / "manifest.json").write_text(json.dumps(manifest))
    with raises(assets.NativeAssetError, match=error), assets.packaged_helper():
        raise AssertionError("Invalid asset yielded")


@mark.parametrize("name", [ASSET, "manifest.json"])
def test_interrupted_asset_manifest_replacement_fails_closed(bundle, name):
    _, packaged, _ = bundle
    if name == ASSET:
        original = (packaged / name).read_bytes()
        (packaged / name).write_bytes(original + b"new generation")
        error = "does not match its manifest"
    else:
        (packaged / name).write_bytes(b'{"format":')
        error = "manifest is malformed"
    with raises(assets.NativeAssetError, match=error), assets.packaged_helper():
        raise AssertionError("Interrupted bundle yielded")


@mark.parametrize("name", [SOURCES[0], "io.h"])
def test_stale_source_or_header_is_rejected_before_yield(bundle, name):
    native, _, _ = bundle
    (native / name).write_bytes(b"changed native input")
    with raises(assets.NativeAssetError, match="stale for this package"), assets.packaged_helper():
        raise AssertionError("Stale bundle yielded")


@mark.parametrize("name", [ASSET, "manifest.json"])
def test_missing_bundle_entry_reports_incomplete_package(bundle, name):
    _, packaged, _ = bundle
    (packaged / name).unlink()
    with (
        raises(assets.NativeAssetError, match="missing its native helper bundle"),
        assets.packaged_helper(),
    ):
        raise AssertionError("Missing bundle yielded")


@mark.parametrize(("cpu", "kind"), [(0x01000007, 2), (0x0100000C, 6)])
def test_matching_digest_does_not_make_wrong_architecture_or_library_executable(bundle, cpu, kind):
    _, packaged, manifest = bundle
    binary = struct.pack("<8I", 0xFEEDFACF, cpu, 0, kind, 0, 0, 0, 0)
    (packaged / ASSET).write_bytes(binary)
    manifest["sha256"] = hashlib.sha256(binary).hexdigest()
    (packaged / "manifest.json").write_text(json.dumps(manifest))
    with (
        raises(assets.NativeAssetError, match="not an arm64 Mach-O executable"),
        assets.packaged_helper(),
    ):
        raise AssertionError("Wrong executable yielded")


def test_oversized_bundle_is_read_only_to_bound(bundle):
    _, packaged, _ = bundle
    with (packaged / ASSET).open("wb") as stream:
        stream.truncate(assets.MAX_HELPER_BYTES + 1)
    with (
        raises(assets.NativeAssetError, match="exceeds its size bounds"),
        assets.packaged_helper(),
    ):
        raise AssertionError("Oversized bundle yielded")
