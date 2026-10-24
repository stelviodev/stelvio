"""Mocked tests for the reproducible Traforo developer builder."""

from __future__ import annotations

import importlib.util
import json
import os
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stelvio.tunnel import assets


@pytest.fixture
def builder(tmp_path, monkeypatch):  # noqa: C901 - fixture assembles an isolated build environment
    script = Path(__file__).parents[2] / "pkg/traforo-client-go/scripts/build.py"
    spec = importlib.util.spec_from_file_location("traforo_developer_builder", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    source = tmp_path / "source"
    (source / ".github/workflows").mkdir(parents=True)
    (source / "cmd/stelvio-traforo").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / ".github/workflows/release.yml").write_text(
        "version: 'v2.18.3'\ngo-version: '1.27.3'\n"
    )
    (source / ".goreleaser.yml").write_text("version: 2\n")
    (source / "go.mod").write_text("module fixture\n")
    (source / "cmd/stelvio-traforo/main.go").write_text("package main\n")
    (source / "scripts/build.py").write_text("# builder\n")
    (source / "README.md").write_text("fixture\n")

    commit = "a" * 40
    snapshot = f"{assets.TRAFORO_VERSION}-dev+g{commit[:12]}"
    images = {
        "arm64": struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0)
        + f"v{snapshot}".encode(),
        "amd64": struct.pack("<8I", 0xFEEDFACF, 0x01000007, 0, 2, 0, 0, 0, 0)
        + f"v{snapshot}".encode(),
    }

    monkeypatch.setattr(module, "_supported", lambda: None)
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/" + name)

    def tool(*args, **kwargs):  # noqa: PLR0911 - deterministic tool response shim
        if "-L" in args:
            return "image:\n /usr/lib/libSystem.B.dylib (version)"
        if args[0] == "/goreleaser":
            return "GitVersion:    2.18.3"
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return commit
        if args[:2] == ("git", "rev-parse"):
            return "/private/tmp/fake-git"
        if args[0] == "/usr/bin/xcrun":
            return "fixture-xcode"
        if args[0] == "/go":
            return "go version go1.27.3 darwin/arm64"
        if args[-1] == "version" and "traforo-darwin" in str(args):
            return f"v{snapshot}"
        return "fixture-tool"

    monkeypatch.setattr(module, "tool", tool)

    def compile_images(arguments, **kwargs):
        output = kwargs["cwd"] / "dist"
        output.mkdir()
        entries = []
        for goarch, data in images.items():
            name = f"traforo-darwin-{goarch}"
            (output / name).write_bytes(data)
            entries.append(
                {
                    "name": name,
                    "path": f"dist/{name}",
                    "goos": "darwin",
                    "goarch": goarch,
                    "extra": {"Format": "binary"},
                }
            )
        (output / "artifacts.json").write_text(json.dumps(entries))
        (output / "metadata.json").write_text(json.dumps({"version": snapshot, "commit": commit}))

    compile_mock = Mock(side_effect=compile_images)
    monkeypatch.setattr(module.subprocess, "run", compile_mock)
    return SimpleNamespace(
        module=module,
        source=source,
        compile=compile_mock,
        images=images,
        commit=commit,
        snapshot=snapshot,
    )


def test_build_validates_and_atomically_publishes_both_architectures(builder):
    path = builder.module.build(builder.source)

    assert path.name == "traforo-darwin-arm64"
    assert path.read_bytes() == builder.images["arm64"]
    assert path.stat().st_mode & 0o777 == 0o755
    assert (path.parent / "traforo-darwin-amd64").read_bytes() == builder.images["amd64"]
    record = json.loads((path.parent / "build.json").read_text())
    names = builder.module.inventory(builder.source)
    assert record["source_sha256"] == builder.module.fingerprint(builder.source, names)
    assert record["commit"] == builder.commit
    assert record["version"] == f"v{builder.snapshot}"
    assert record["go_version"] == "1.27.3"
    assert record["goreleaser"] == "v2.18.3"
    assert {item["architecture"] for item in record["artifacts"]} == {"arm64", "x86_64"}
    assert all(item["sha256"] for item in record["artifacts"])
    assert builder.compile.call_args.args[0] == [
        "/goreleaser",
        "release",
        "--snapshot",
        "--clean",
        "--skip=publish,announce",
    ]
    environment = builder.compile.call_args.kwargs["env"]
    assert environment["GORELEASER_CURRENT_TAG"] == f"v{assets.TRAFORO_VERSION}"
    assert environment["TRAFORO_SNAPSHOT_VERSION"] == builder.snapshot
    assert "GITHUB_TOKEN" not in environment
    assert not list(path.parent.parent.glob(".traforo-*"))


@pytest.mark.parametrize("missing", ["go", "goreleaser"])
def test_missing_tool_fails_before_creating_build_directory(builder, monkeypatch, missing):
    monkeypatch.setattr(
        builder.module.shutil,
        "which",
        lambda name: None if name == missing else "/" + name,
    )
    label = "Go" if missing == "go" else "GoReleaser"

    with pytest.raises(RuntimeError, match=f"{label} was not found on PATH"):
        builder.module.build(builder.source)

    builder.compile.assert_not_called()
    assert not (builder.source / "build").exists()


def test_wrong_goreleaser_version_fails_before_build(builder, monkeypatch):
    original_tool = builder.module.tool

    def wrong_goreleaser(*args, **kwargs):
        if args[0] == "/goreleaser":
            return "GitVersion: 2.18.2"
        return original_tool(*args, **kwargs)

    monkeypatch.setattr(builder.module, "tool", wrong_goreleaser)

    with pytest.raises(RuntimeError, match=r"matching release\.yml"):
        builder.module.build(builder.source)

    builder.compile.assert_not_called()
    assert not (builder.source / "build").exists()


@pytest.mark.parametrize("version", ["1.27.1", "1.27.2"])
def test_wrong_go_version_fails_before_build(builder, monkeypatch, version):
    original_tool = builder.module.tool

    def wrong_go(*args, **kwargs):
        if args[0] == "/go":
            return f"go version go{version} darwin/arm64"
        return original_tool(*args, **kwargs)

    monkeypatch.setattr(builder.module, "tool", wrong_go)

    with pytest.raises(RuntimeError, match=r"Install Go 1\.27\.3, matching release\.yml"):
        builder.module.build(builder.source)

    builder.compile.assert_not_called()
    assert not (builder.source / "build").exists()


@pytest.mark.parametrize(
    "mutation",
    ["wrong-os", "wrong-arch", "wrong-format", "duplicate", "escaped-path"],
)
def test_release_artifact_metadata_must_select_one_confined_binary(tmp_path, builder, mutation):
    captured = tmp_path / "captured"
    dist = captured / "dist"
    dist.mkdir(parents=True)
    image = dist / "traforo-darwin-arm64"
    image.write_bytes(builder.images["arm64"])
    entry = {
        "name": "traforo-darwin-arm64",
        "path": "dist/traforo-darwin-arm64",
        "goos": "darwin",
        "goarch": "arm64",
        "extra": {"Format": "binary"},
    }
    if mutation == "wrong-os":
        entry["goos"] = "linux"
    elif mutation == "wrong-arch":
        entry["goarch"] = "amd64"
    elif mutation == "wrong-format":
        entry["extra"]["Format"] = "archive"
    elif mutation == "duplicate":
        (dist / "duplicate").write_bytes(builder.images["arm64"])
        duplicate = dict(entry, path="dist/duplicate")
        (dist / "artifacts.json").write_text(json.dumps([entry, duplicate]))
    elif mutation == "escaped-path":
        (captured / "outside").write_bytes(builder.images["arm64"])
        entry["path"] = "../outside"
    if mutation != "duplicate":
        (dist / "artifacts.json").write_text(json.dumps([entry]))

    with pytest.raises(RuntimeError, match="artifact"):
        builder.module.release_binary(captured, "traforo-darwin-arm64", "arm64")


@pytest.mark.parametrize(
    "failure",
    [
        "compile",
        "source-change",
        "config-change",
        "architecture",
        "missing-image",
        "version",
        "library",
        "metadata-version",
        "metadata-commit",
    ],
)
def test_failed_build_preserves_existing_bundle(builder, monkeypatch, failure):  # noqa: C901 - independent failure injection cases
    module = builder.module
    output = builder.source / "build"
    previous = output / "traforo-previous/traforo-darwin-arm64"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"previous")
    compile_images = builder.compile.side_effect
    original_tool = module.tool

    def compile_with_failure(arguments, **kwargs):
        compile_images(arguments, **kwargs)
        output_dir = kwargs["cwd"] / "dist"
        if failure == "source-change":
            (builder.source / "cmd/stelvio-traforo/main.go").write_text("changed\n")
        elif failure == "config-change":
            (builder.source / ".goreleaser.yml").write_text("changed\n")
        elif failure == "architecture":
            (output_dir / "traforo-darwin-amd64").write_bytes(builder.images["arm64"])
        elif failure == "missing-image":
            (output_dir / "traforo-darwin-amd64").unlink()
        elif failure in {"metadata-version", "metadata-commit"}:
            metadata_path = output_dir / "metadata.json"
            metadata = json.loads(metadata_path.read_text())
            metadata["version" if failure == "metadata-version" else "commit"] = "wrong"
            metadata_path.write_text(json.dumps(metadata))

    def validate_tool(*args, **kwargs):
        if failure == "version" and args[-1] == "version" and "traforo-darwin" in str(args):
            return "v0.0.6"
        if failure == "library" and "-L" in args:
            return "image:\n /usr/local/lib/foreign.dylib (version)"
        return original_tool(*args, **kwargs)

    monkeypatch.setattr(module, "tool", validate_tool)
    builder.compile.side_effect = compile_with_failure
    if failure == "compile":
        builder.compile.side_effect = subprocess.CalledProcessError(1, "goreleaser")

    with pytest.raises(
        (
            RuntimeError,
            assets.NativeAssetError,
            subprocess.CalledProcessError,
            FileNotFoundError,
        )
    ):
        module.build(builder.source)

    assert previous.read_bytes() == b"previous"
    assert list(output.iterdir()) == [previous.parent]


def test_builder_rejects_source_symlinks(builder):
    link = builder.source / "cmd/stelvio-traforo/linked.go"
    link.symlink_to(builder.source / "cmd/stelvio-traforo/main.go")

    with pytest.raises(RuntimeError, match="regular file"):
        builder.module.inventory(builder.source)


def test_builder_main_build_only_never_installs(builder, monkeypatch, capsys, tmp_path):
    binary = tmp_path / "traforo-darwin-arm64"
    binary.write_bytes(b"candidate")
    monkeypatch.setattr(builder.module, "build", lambda source: binary)
    monkeypatch.setattr(
        builder.module,
        "select_traforo",
        lambda environment: SimpleNamespace(digest="d" * 64),
    )
    install = Mock(side_effect=AssertionError("build-only authorized installation"))
    monkeypatch.setattr(builder.module, "install_helper", install)

    builder.module.main(["--build-only"])

    install.assert_not_called()
    assert f"export {assets.DEVELOPMENT_BINARY_ENV}={binary}" in capsys.readouterr().out


def test_builder_main_installs_only_through_existing_transaction(
    builder, monkeypatch, capsys, tmp_path
):
    binary = tmp_path / "traforo image"
    binary.write_bytes(b"candidate")
    monkeypatch.setenv(assets.DEVELOPMENT_BINARY_ENV, "/previous/override")
    monkeypatch.setattr(builder.module, "build", lambda source: binary)
    monkeypatch.setattr(
        builder.module,
        "select_traforo",
        lambda environment: SimpleNamespace(digest="d" * 64),
    )
    install = Mock()
    monkeypatch.setattr(builder.module, "install_helper", install)

    builder.module.main([])

    install.assert_called_once_with(
        environment={**os.environ, assets.DEVELOPMENT_BINARY_ENV: str(binary)}
    )
    assert os.environ[assets.DEVELOPMENT_BINARY_ENV] == "/previous/override"
    assert f"export {assets.DEVELOPMENT_BINARY_ENV}='{binary}'" in capsys.readouterr().out


def test_builder_main_does_not_install_after_build_failure(builder, monkeypatch):
    monkeypatch.setattr(builder.module, "build", Mock(side_effect=RuntimeError("build failed")))
    install = Mock(side_effect=AssertionError("failed build installed helper"))
    monkeypatch.setattr(builder.module, "install_helper", install)

    with pytest.raises(RuntimeError, match="build failed"):
        builder.module.main([])

    install.assert_not_called()


@pytest.fixture(autouse=True)
def _darwin_fixture_platform(monkeypatch):
    """Synthetic Mach-O and macOS command fixtures retain their explicit platform."""
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
