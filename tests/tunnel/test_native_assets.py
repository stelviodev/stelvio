"""Both precompiled architectures must have coherent bytes and source manifests."""

import hashlib
import importlib.util
import json
import struct
from pathlib import Path

from pytest import fixture, mark, raises

from stelvio.tunnel import assets


@fixture
def bundle(tmp_path, monkeypatch):
    packaged = tmp_path / "_assets"
    packaged.mkdir()
    monkeypatch.setattr(assets, "files", lambda _: tmp_path)
    monkeypatch.setattr(assets.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(assets.platform, "machine", lambda: "arm64")
    targets = {}
    for architecture, cpu in assets.MACHO_CPUS.items():
        binary = struct.pack("<8I", 0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0) + b"fixture"
        name = f"stelvio-traforo-macos-{architecture}"
        (packaged / name).write_bytes(binary)
        targets[architecture] = dict(  # noqa: C408 - target fixture fields
            asset=name,
            architecture=architecture,
            minimum_macos="15.0",
            sha256=hashlib.sha256(binary).hexdigest(),
            bytes=len(binary),
            direct_libraries=["/usr/lib/libSystem.B.dylib"],
        )
    manifest = dict(  # noqa: C408 - manifest fixture fields
        format=2,
        helper_abi=1,
        carrier_version=1,
        source_inventory=["main.go"],
        source_sha256=hashlib.sha256(b"main.go\0package main\n\0").hexdigest(),
        targets=targets,
    )
    (packaged / "traforo.json").write_text(json.dumps(manifest))
    return packaged, manifest


@mark.parametrize("architecture", ["arm64", "x86_64"])
def test_coherent_bundle_selects_architecture_without_execution(bundle, monkeypatch, architecture):
    packaged, _ = bundle
    monkeypatch.setattr(assets.platform, "machine", lambda: architecture)
    with assets.packaged_helper() as path:
        assert path == packaged / f"stelvio-traforo-macos-{architecture}"
        assert path.read_bytes().endswith(b"fixture")


@mark.parametrize(
    ("key", "value"),
    [
        ("format", True),
        ("helper_abi", 2),
        ("carrier_version", "1"),
        ("source_sha256", "short"),
        ("source_inventory", ["../main.go"]),
    ],
)
def test_incompatible_manifest_fails_closed(bundle, key, value):
    packaged, manifest = bundle
    manifest[key] = value
    (packaged / "traforo.json").write_text(json.dumps(manifest))
    with raises(assets.NativeAssetError), assets.packaged_helper():
        raise AssertionError("Invalid asset yielded")


@mark.parametrize(
    ("key", "value"),
    [
        ("asset", "../../injected"),
        ("architecture", "linux"),
        ("direct_libraries", ["/unexpected.dylib"]),
        ("sha256", "A" * 64),
        ("bytes", 17 * 1024 * 1024),
    ],
)
def test_target_cannot_select_foreign_paths_or_dependencies(bundle, key, value):
    packaged, manifest = bundle
    manifest["targets"]["arm64"][key] = value
    (packaged / "traforo.json").write_text(json.dumps(manifest))
    with raises(assets.NativeAssetError), assets.packaged_helper():
        raise AssertionError("Invalid target yielded")


def test_wrong_macho_cpu_is_rejected_even_with_matching_digest(bundle):
    packaged, manifest = bundle
    target = manifest["targets"]["arm64"]
    data = struct.pack("<8I", 0xFEEDFACF, 0x01000007, 0, 2, 0, 0, 0, 0) + b"fixture"
    (packaged / target["asset"]).write_bytes(data)
    target["sha256"] = hashlib.sha256(data).hexdigest()
    (packaged / "traforo.json").write_text(json.dumps(manifest))
    with raises(assets.NativeAssetError, match="matching Mach-O"), assets.packaged_helper():
        raise AssertionError("Wrong CPU yielded")


@mark.parametrize("change", ["bytes", "missing", "manifest"])
def test_interrupted_or_stale_bundle_is_rejected(bundle, change):
    packaged, _ = bundle
    asset = packaged / "stelvio-traforo-macos-arm64"
    if change == "bytes":
        asset.write_bytes(asset.read_bytes() + b"changed")
    elif change == "missing":
        asset.unlink()
    else:
        (packaged / "traforo.json").write_bytes(b'{"format":')
    with raises(assets.NativeAssetError), assets.packaged_helper():
        raise AssertionError("Invalid bundle yielded")


def test_repository_release_assets_match_current_go_source():
    repository = Path(__file__).parents[2]
    builder_path = repository / "pkg/traforo-client-go/build.py"
    spec = importlib.util.spec_from_file_location("traforo_release_builder", builder_path)
    assert spec is not None
    assert spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    builder.check()


def test_unsupported_platform_refused_before_asset_read(monkeypatch):
    monkeypatch.setattr(assets.platform, "system", lambda: "Linux")
    with raises(assets.NativeAssetError, match="supports macOS"), assets.packaged_helper():
        raise AssertionError("Unsupported platform yielded")
