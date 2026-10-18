"""Explicit developer images never weaken pinned release downloads."""

import hashlib
import importlib.util
import json
import os
import shlex
import struct
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from pytest import fixture, mark, raises

from stelvio.tunnel import assets, installation


@fixture
def local_image(tmp_path, monkeypatch):
    image = tmp_path / "local image"
    image.write_bytes(struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0))
    image.chmod(0o755)
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    monkeypatch.setattr(
        assets.subprocess,
        "check_output",
        lambda *args, **kwargs: b"-rwxr-xr-x 1 developer staff 32 local-image\n",
    )
    monkeypatch.setattr(
        assets.subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(
                returncode=0, stdout=assets.EXPECTED_VERSION.encode(), stderr=b""
            )
        ),
    )
    return image


@mark.parametrize("value", [None, ""])
def test_default_selection_keeps_release_pin_without_cache_or_network(monkeypatch, value):
    monkeypatch.setattr(assets, "_architecture", lambda: "arm64")
    monkeypatch.setattr(assets, "ensure_traforo", Mock(side_effect=AssertionError("download")))
    selected = assets.select_traforo(
        {} if value is None else {assets.DEVELOPMENT_BINARY_ENV: value}
    )
    assert selected.digest == assets.TARGETS["arm64"]["sha256"]
    assert selected.local_path is None
    assert selected.notice is None


def test_explicit_image_is_selected_without_changing_pins_or_environment(local_image, monkeypatch):
    before = {k: dict(v) for k, v in assets.TARGETS.items()}
    monkeypatch.setenv(assets.DEVELOPMENT_BINARY_ENV, "parent-mutation")
    selected = assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(local_image)})
    assert selected.digest == hashlib.sha256(local_image.read_bytes()).hexdigest()
    assert selected.local_path == local_image
    assert selected.digest[:12] in selected.notice
    assert before == assets.TARGETS
    assert os.environ[assets.DEVELOPMENT_BINARY_ENV] == "parent-mutation"


@mark.parametrize(
    "bad",
    [
        "relative",
        "missing",
        "symlink",
        "hardlink",
        "directory",
        "writable",
        "special",
        "nonexecutable",
        "foreign",
        "cpu",
        "size",
        "protocol",
    ],
)
def test_invalid_override_never_falls_back_or_changes_helper(local_image, monkeypatch, bad):  # noqa: C901 - independent refusal cases
    path = local_image
    if bad == "relative":
        path = Path("relative")
    elif bad == "missing":
        path = path.with_name("missing")
    elif bad == "symlink":
        path = path.with_name("link")
        path.symlink_to(local_image)
    elif bad == "hardlink":
        os.link(local_image, local_image.with_name("second"))
    elif bad == "directory":
        path = path.parent
    elif bad == "writable":
        path.chmod(0o777)
    elif bad == "special":
        path.chmod(0o4755)
    elif bad == "nonexecutable":
        path.chmod(0o644)
    elif bad == "foreign":
        monkeypatch.setattr(assets.os, "geteuid", lambda: local_image.stat().st_uid + 1)
    elif bad == "cpu":
        monkeypatch.setattr(assets, "MACHO_CPUS", {"arm64": 123})
    elif bad == "size":
        path.write_bytes(b"short")
    elif bad == "protocol":
        assets.subprocess.run.return_value.stdout = b"incompatible"
    monkeypatch.setattr(installation, "_supported", lambda: None)
    authorize = Mock(side_effect=AssertionError("host mutation"))
    plugin = Mock(side_effect=AssertionError("plugin installation"))
    monkeypatch.setattr(installation, "_authorize", authorize)
    monkeypatch.setattr(installation, "ensure_plugin", plugin)
    with raises(assets.NativeAssetError):
        installation.install_helper(environment={assets.DEVELOPMENT_BINARY_ENV: str(path)})
    authorize.assert_not_called()
    plugin.assert_not_called()


def test_image_changed_during_version_check_is_refused(local_image):
    def mutate(*args, **kwargs):
        local_image.write_bytes(local_image.read_bytes() + b"changed")
        return SimpleNamespace(returncode=0, stdout=assets.EXPECTED_VERSION.encode(), stderr=b"")

    assets.subprocess.run.side_effect = mutate
    with raises(assets.NativeAssetError, match="changed during validation"):
        assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(local_image)})


@fixture
def builder(tmp_path, monkeypatch):
    script = Path(__file__).parents[2] / "pkg/traforo-client-go/scripts/build.py"
    spec = importlib.util.spec_from_file_location("traforo_developer_builder", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    (source / "cmd/stelvio-traforo").mkdir(parents=True)
    (source / ".github/workflows").mkdir(parents=True)
    (source / ".github/workflows/release.yml").write_text("version: v2.18.3\n")
    (source / ".goreleaser.yml").write_text("version: 2\n")
    (source / "go.mod").write_text("module fixture\n")
    (source / "cmd/stelvio-traforo/main.go").write_text("package main\n")
    images = {
        "arm64": struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0),
        "amd64": struct.pack("<8I", 0xFEEDFACF, 0x01000007, 0, 2, 0, 0, 0, 0),
    }
    monkeypatch.setattr(module, "_supported", lambda: None)
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(module.shutil, "which", lambda name: "/" + name)

    def tool(*args, **kwargs):
        if "-L" in args:
            return "image:\n /usr/lib/libSystem.B.dylib (version)"
        if args[0] == "/goreleaser":
            return "GitVersion:    2.18.3"
        if args[:3] == ("git", "rev-parse", "HEAD"):
            return "a" * 40
        if args[-1] == "version" and "traforo-darwin" in str(args):
            return "v0.0.7-dev+g" + "a" * 12
        return "fixture-toolchain"

    monkeypatch.setattr(module, "tool", tool)

    def compile_image(arguments, **kwargs):
        output = kwargs["cwd"] / "dist"
        output.mkdir()
        for arch, data in images.items():
            (output / f"traforo-darwin-{arch}").write_bytes(data)
        (output / "metadata.json").write_text(
            json.dumps(
                {
                    "version": "0.0.7-dev+g" + "a" * 12,
                    "commit": "a" * 40,
                }
            )
        )
        (output / "artifacts.json").write_text(
            json.dumps(
                [
                    {
                        "name": f"traforo-darwin-{arch}",
                        "path": f"dist/traforo-darwin-{arch}",
                        "goos": "darwin",
                        "goarch": arch,
                        "extra": {"Format": "binary"},
                    }
                    for arch in images
                ]
            )
        )

    compile_mock = Mock(side_effect=compile_image)
    monkeypatch.setattr(module.subprocess, "run", compile_mock)
    return SimpleNamespace(module=module, source=source, compile=compile_mock, images=images)


def test_developer_build_uses_shared_release_config_and_publishes_both_images(builder):
    path = builder.module.build(builder.source)
    assert path.name == "traforo-darwin-arm64"
    assert path.read_bytes() == builder.images["arm64"]
    assert path.stat().st_mode & 0o777 == 0o755
    assert (path.parent / "traforo-darwin-amd64").read_bytes() == builder.images["amd64"]
    record = json.loads((path.parent / "build.json").read_text())
    assert record["source_sha256"] == builder.module.fingerprint(
        builder.source, builder.module.inventory(builder.source)
    )
    assert record["commit"] == "a" * 40
    assert record["version"] == "v0.0.7-dev+g" + "a" * 12
    assert len(record["artifacts"]) == 2
    for artifact in record["artifacts"]:
        assert (
            artifact["sha256"]
            == hashlib.sha256((path.parent / artifact["name"]).read_bytes()).hexdigest()
        )
    assert builder.compile.call_args.args[0] == [
        "/goreleaser",
        "release",
        "--snapshot",
        "--clean",
        "--skip=publish,announce",
    ]
    environment = builder.compile.call_args.kwargs["env"]
    assert environment["GORELEASER_CURRENT_TAG"] == "v0.0.7"
    assert environment["TRAFORO_SNAPSHOT_VERSION"] == "0.0.7-dev+g" + "a" * 12
    assert "GITHUB_TOKEN" not in environment
    assert not list(path.parent.parent.glob(".traforo-*"))


@mark.parametrize(
    "failure",
    [
        "compile",
        "version",
        "library",
        "source-change",
        "config-change",
        "architecture",
        "missing-image",
    ],
)
def test_failed_build_preserves_previous_output_without_installation(
    builder, monkeypatch, failure
):
    module, source = builder.module, builder.source
    previous = source / "build/traforo-previous/traforo-darwin-arm64"
    previous.parent.mkdir(parents=True)
    previous.write_bytes(b"previous")
    original_compile = builder.compile.side_effect
    original_tool = module.tool

    def compile_image(*args, **kwargs):
        original_compile(*args, **kwargs)
        if failure in {"source-change", "config-change"}:
            (source / ("go.mod" if failure == "source-change" else ".goreleaser.yml")).write_text(
                "changed"
            )
        if failure == "architecture":
            (kwargs["cwd"] / "dist/traforo-darwin-amd64").write_bytes(builder.images["arm64"])
        if failure == "missing-image":
            (kwargs["cwd"] / "dist/traforo-darwin-amd64").unlink()

    def tool(*args, **kwargs):
        if failure == "version" and args[-1] == "version" and "traforo-darwin" in str(args):
            return "v0.0.4"
        if failure == "library" and "-L" in args:
            return "image:\n /foreign/library (v)"
        return original_tool(*args, **kwargs)

    monkeypatch.setattr(module, "tool", tool)
    builder.compile.side_effect = compile_image
    if failure == "compile":
        builder.compile.side_effect = subprocess.CalledProcessError(1, "goreleaser")
    with raises((RuntimeError, subprocess.CalledProcessError, FileNotFoundError)):
        module.build(source)
    assert previous.read_bytes() == b"previous"
    assert list(previous.parent.parent.iterdir()) == [previous.parent]


@mark.parametrize("missing", ["go", "goreleaser"])
def test_missing_build_tool_identifies_prerequisite(builder, monkeypatch, missing):
    monkeypatch.setattr(
        builder.module.shutil, "which", lambda name: None if name == missing else "/" + name
    )
    label = "Go" if missing == "go" else "GoReleaser"
    expected = f"{label} was not found on PATH"
    with raises(RuntimeError, match=expected):
        builder.module.build(builder.source)
    builder.compile.assert_not_called()
    assert not (builder.source / "build").exists()


def test_wrong_goreleaser_version_fails_before_build(builder, monkeypatch):
    monkeypatch.setattr(builder.module, "tool", lambda *a, **kw: "GitVersion:    2.17.0")
    with raises(RuntimeError, match=r"matching release\.yml"):
        builder.module.build(builder.source)
    builder.compile.assert_not_called()


def test_install_uses_local_image_then_unsetting_selects_release(
    local_image, monkeypatch, tmp_path
):
    from contextlib import contextmanager

    from stelvio.tunnel.helper_client import HelperInspection

    monkeypatch.setattr(installation, "_supported", lambda: None)
    monkeypatch.setattr(installation, "ensure_plugin", lambda: None)
    monkeypatch.setattr(installation, "_installed_digest", lambda: None)
    monkeypatch.setattr(installation, "_wait_ready", lambda: None)
    calls = []
    monkeypatch.setattr(installation, "_authorize", calls.append)
    download = Mock(side_effect=AssertionError("Developer selection downloaded release"))
    monkeypatch.setattr(installation, "downloaded_helper", download)
    selected = installation.install_helper(
        environment={assets.DEVELOPMENT_BINARY_ENV: str(local_image)}
    )
    assert selected.local_path == local_image
    assert selected.digest in calls[0]
    installed = tmp_path / "installed"
    installed.write_bytes(local_image.read_bytes())
    monkeypatch.setattr(installation, "INSTALLED_HELPER", installed)
    monkeypatch.setattr(installation, "_installed_digest", lambda: selected.digest)
    monkeypatch.setattr(
        installation,
        "NativeHelper",
        lambda: SimpleNamespace(inspect=lambda: HelperInspection(False, False, False)),
    )
    installation.install_helper(environment={assets.DEVELOPMENT_BINARY_ENV: str(local_image)})
    assert len(calls) == 1
    download.assert_not_called()
    release = tmp_path / "release"
    release.write_bytes(b"pinned-release")

    @contextmanager
    def downloaded():
        yield release

    monkeypatch.setattr(installation, "downloaded_helper", downloaded)
    restored = installation.install_helper(environment={})
    assert restored.local_path is None
    assert restored.digest == assets.expected_digest()
    assert len(calls) == 2
    assert restored.digest in calls[1]


def test_builder_main_prints_export_and_scopes_override_to_install(
    builder, local_image, monkeypatch, capsys
):
    module = builder.module
    monkeypatch.setenv(assets.DEVELOPMENT_BINARY_ENV, "/previous/environment")
    monkeypatch.setattr(module, "build", lambda _: local_image)
    install = Mock()
    monkeypatch.setattr(module, "install_helper", install)
    module.main([])
    assert install.call_args.kwargs["environment"][assets.DEVELOPMENT_BINARY_ENV] == str(
        local_image
    )
    assert os.environ[assets.DEVELOPMENT_BINARY_ENV] == "/previous/environment"
    assert (
        f"export {assets.DEVELOPMENT_BINARY_ENV}={shlex.quote(str(local_image))}"
        in capsys.readouterr().out
    )


def test_builder_main_does_not_install_when_build_fails(builder, monkeypatch):
    monkeypatch.setattr(builder.module, "build", Mock(side_effect=RuntimeError("build failed")))
    install = Mock()
    monkeypatch.setattr(builder.module, "install_helper", install)
    with raises(RuntimeError, match="build failed"):
        builder.module.main([])
    install.assert_not_called()


@mark.parametrize(
    "output", [b"-rwxr-xr-x+ image\n 0: everyone allow write\n", b"", b"-rwxr-xr-x+ image\n"]
)
def test_developer_acl_refused_before_execution(local_image, monkeypatch, output):
    monkeypatch.setattr(assets.subprocess, "check_output", lambda *a, **kw: output)
    with raises(assets.NativeAssetError, match="ACL"):
        assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(local_image)})
    assets.subprocess.run.assert_not_called()


def test_permission_change_during_validation_refused(local_image):
    def mutate(*args, **kwargs):
        local_image.chmod(0o777)
        return SimpleNamespace(returncode=0, stdout=assets.EXPECTED_VERSION.encode(), stderr=b"")

    assets.subprocess.run.side_effect = mutate
    with raises(assets.NativeAssetError, match="permissions"):
        assets.select_traforo({assets.DEVELOPMENT_BINARY_ENV: str(local_image)})


def test_builder_help_has_no_build_or_install_side_effect(builder, monkeypatch):
    build = Mock(side_effect=AssertionError("Unexpected build"))
    install = Mock(side_effect=AssertionError("Unexpected install"))
    monkeypatch.setattr(builder.module, "build", build)
    monkeypatch.setattr(builder.module, "install_helper", install)
    with raises(SystemExit) as result:
        builder.module.main(["--help"])
    assert result.value.code == 0
    build.assert_not_called()
    install.assert_not_called()


@mark.parametrize(
    "output",
    [
        b"v0.0.7\n",
        b"v0.0.7-dev+gabcdef123456\n",
        b"v0.0.6\n",
        b"v0.0.5\n",
        b"v0.0.5-dev+gabcdef123456\n",
        b"v0.0.7-dev\n",
        b"v0.0.7-dev+gNOTHEX\n",
        b"stelvio-traforo/1 helper=1 carrier=1\n",
    ],
)
def test_developer_semantic_version_acceptance(local_image, output):
    assets.subprocess.run.return_value.stdout = output
    environment = {assets.DEVELOPMENT_BINARY_ENV: str(local_image)}
    if output in {b"v0.0.7\n", b"v0.0.7-dev+gabcdef123456\n"}:
        assets.require_network_version(environment)
    else:
        with raises(assets.NativeAssetError, match="must report"):
            assets.require_network_version(environment)


def test_builder_build_only_does_not_install(builder, local_image, monkeypatch, capsys):
    monkeypatch.setattr(builder.module, "build", lambda _: local_image)
    install = Mock()
    monkeypatch.setattr(builder.module, "install_helper", install)
    builder.module.main(["--build-only"])
    install.assert_not_called()
    assert "export STLV_TRAFORO_DEV_BINARY=" in capsys.readouterr().out


@mark.parametrize("host", ["arm64", "x86_64"])
def test_builder_records_cross_architecture_validation(builder, monkeypatch, host):
    module = builder.module
    monkeypatch.setattr(module.platform, "machine", lambda: host)
    # Linker-injected snapshot string enables static arm64 verification on Intel.
    if host == "x86_64":
        builder.images["arm64"] += b"v0.0.7-dev+g" + b"a" * 12
    path = module.build(builder.source)
    record = json.loads((path.parent / "build.json").read_text())
    executions = {a["architecture"]: a["execution"] for a in record["artifacts"]}
    assert executions == (
        {"arm64": "native", "x86_64": "Rosetta"}
        if host == "arm64"
        else {"arm64": "pending-native-arm64", "x86_64": "native"}
    )
    assert path.name == ("traforo-darwin-arm64" if host == "arm64" else "traforo-darwin-amd64")


def test_missing_rosetta_preserves_previous_output(builder, monkeypatch):
    original = builder.module.tool

    def missing(*args, **kwargs):
        if args[:2] == ("/usr/bin/arch", "-x86_64"):
            raise subprocess.CalledProcessError(1, args)
        return original(*args, **kwargs)

    monkeypatch.setattr(builder.module, "tool", missing)
    with raises(RuntimeError, match="install Rosetta"):
        builder.module.build(builder.source)
    assert not list((builder.source / "build").iterdir())


@mark.parametrize("field", ["version", "commit"])
def test_mismatched_goreleaser_metadata_never_publishes(builder, field):
    original = builder.compile.side_effect

    def mismatch(*args, **kwargs):
        original(*args, **kwargs)
        path = kwargs["cwd"] / "dist/metadata.json"
        metadata = json.loads(path.read_text())
        metadata[field] = "wrong"
        path.write_text(json.dumps(metadata))

    builder.compile.side_effect = mismatch
    with raises(RuntimeError, match="metadata differs"):
        builder.module.build(builder.source)
    assert not list((builder.source / "build").iterdir())
