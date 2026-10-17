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
    (source / "go.mod").write_text("module fixture\n")
    (source / "cmd/stelvio-traforo/main.go").write_text("package main\n")
    binary = struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0)
    monkeypatch.setattr(module, "_supported", lambda: None)
    monkeypatch.setattr(module.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(module.shutil, "which", lambda _: "/go")
    monkeypatch.setattr(module, "validate_protocol", lambda _: None)

    def tool(*args, **kwargs):
        if "-L" in args:
            return "image:\n /usr/lib/libSystem.B.dylib (version)"
        return "fixture-toolchain"

    monkeypatch.setattr(module, "tool", tool)

    def compile_image(arguments, **kwargs):
        Path(arguments[arguments.index("-o") + 1]).write_bytes(binary)

    compile_mock = Mock(side_effect=compile_image)
    monkeypatch.setattr(module.subprocess, "run", compile_mock)
    return SimpleNamespace(module=module, source=source, compile=compile_mock, binary=binary)


def test_developer_build_captures_source_and_publishes_host_image(builder):
    path = builder.module.build(builder.source)
    assert path == builder.source / "build/stelvio-traforo-arm64"
    assert path.read_bytes() == builder.binary
    assert path.stat().st_mode & 0o777 == 0o755
    record = json.loads((path.parent / (path.name + ".json")).read_text())
    assert record["sha256"] == hashlib.sha256(builder.binary).hexdigest()
    assert record["source_sha256"] == builder.module.fingerprint(
        builder.source, builder.module.inventory(builder.source)
    )
    command = builder.compile.call_args.args[0]
    assert all(
        flag in command
        for flag in ("-mod=readonly", "-trimpath", "-buildvcs=false", "-ldflags=-s -w")
    )
    environment = builder.compile.call_args.kwargs["env"]
    assert environment["CGO_ENABLED"] == "1"
    assert environment["GOARCH"] == "arm64"
    assert not list(path.parent.glob(".traforo-*"))


@mark.parametrize("failure", ["compile", "protocol", "library", "source-change"])
def test_failed_build_preserves_previous_output_without_installation(
    builder, monkeypatch, failure
):
    module, source = builder.module, builder.source
    output = source / "build/stelvio-traforo-arm64"
    output.parent.mkdir()
    output.write_bytes(b"previous")
    if failure == "compile":
        builder.compile.side_effect = subprocess.CalledProcessError(1, "go")
    elif failure == "protocol":
        monkeypatch.setattr(
            module, "validate_protocol", Mock(side_effect=assets.NativeAssetError("protocol"))
        )
    elif failure == "library":
        monkeypatch.setattr(module, "tool", lambda *args, **kw: "image:\n /foreign/library (v)")
    else:
        original = builder.compile.side_effect

        def changed(*args, **kwargs):
            original(*args, **kwargs)
            (source / "go.mod").write_text("changed")

        builder.compile.side_effect = changed
    with raises((RuntimeError, subprocess.CalledProcessError)):
        module.build(source)
    assert output.read_bytes() == b"previous"
    assert not list(output.parent.glob(".traforo-*"))


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
