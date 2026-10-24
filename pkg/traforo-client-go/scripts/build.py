# ruff: noqa: INP001, T201 - standalone developer tool in the split Go module.
"""Build, verify, and optionally install this checkout's Traforo images."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

from stelvio.tunnel.assets import (
    DEVELOPMENT_BINARY_ENV,
    TRAFORO_VERSION,
    NativeAssetError,
    _architecture,
    select_traforo,
    validate_executable,
)
from stelvio.tunnel.installation import _supported, install_helper

LIBRARIES = frozenset(
    {
        "/usr/lib/libbsm.0.dylib",
        "/usr/lib/libSystem.B.dylib",
        "/usr/lib/libresolv.9.dylib",
        "/System/Library/Frameworks/SystemConfiguration.framework/Versions/A/SystemConfiguration",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
        "/System/Library/Frameworks/Security.framework/Versions/A/Security",
    }
)
INVENTORY_NAMES = frozenset(
    {
        ".goreleaser.yml",
        ".goreleaser-linux.yml",
        ".gitattributes",
        "build.py",
        "CODE_OF_CONDUCT.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "Makefile",
        "README.md",
        "go.mod",
        "go.sum",
        "release.yml",
    }
)


def inventory(source: Path) -> tuple[str, ...]:
    """List build source and settings while excluding generated output."""
    names: list[str] = []
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if {"build", "dist", ".git"}.intersection(relative.parts):
            continue
        relevant = path.suffix in {".go", ".c", ".h", ".json"} or path.name in INVENTORY_NAMES
        if not relevant:
            continue
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Traforo build input is not a regular file: {relative}")
        try:
            path.resolve(strict=True).relative_to(source.resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            raise RuntimeError(f"Traforo build input escapes the module: {relative}") from None
        names.append(relative.as_posix())
    return tuple(sorted(names))


def fingerprint(source: Path, names: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + (source / name).read_bytes() + b"\0")
    return digest.hexdigest()


def tool(
    *arguments: str,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
) -> str:
    return subprocess.check_output(  # noqa: S603 - fixed local build/inspection tools
        arguments,
        cwd=cwd,
        env=environment,
        text=True,
        timeout=60,
    ).strip()


def goreleaser_version(source: Path) -> str:
    workflow = (source / ".github/workflows/release.yml").read_text()
    match = re.search(r"(?m)^\s*version:\s*['\"]?(v[0-9]+\.[0-9]+\.[0-9]+)['\"]?\s*$", workflow)
    if not match:
        raise RuntimeError("Cannot find the pinned GoReleaser version in release.yml")
    return match[1]


def go_version(source: Path) -> str:
    workflow = (source / ".github/workflows/release.yml").read_text()
    match = re.search(
        r"(?m)^\s*go-version:\s*['\"]?([0-9]+\.[0-9]+\.[0-9]+)['\"]?\s*$",
        workflow,
    )
    if not match:
        raise RuntimeError("Cannot find the pinned Go version in release.yml")
    return match[1]


def build_tools(source: Path) -> tuple[str, str, str, str]:
    """Require the release toolchain before creating local output."""
    _supported()
    compiler = shutil.which("go")
    releaser = shutil.which("goreleaser")
    required_go = go_version(source)
    required = goreleaser_version(source)
    if compiler is None:
        raise RuntimeError("Go was not found on PATH; install Go compatible with go.mod")
    reported_go = tool(compiler, "version")
    if not re.search(r"(?m)^go version go" + re.escape(required_go) + r"\s", reported_go):
        raise RuntimeError(f"Install Go {required_go}, matching release.yml")
    if releaser is None:
        raise RuntimeError(
            f"GoReleaser was not found on PATH; install GoReleaser {required} from "
            f"https://github.com/goreleaser/goreleaser/releases/tag/{required} "
            "and add its executable directory to PATH"
        )
    reported = tool(releaser, "--version")
    if not re.search(
        r"(?m)^GitVersion:\s*" + re.escape(required.removeprefix("v")) + r"\s*$",
        reported,
    ):
        raise RuntimeError(f"Install GoReleaser {required}, matching release.yml")
    return compiler, releaser, required_go, required


def release_binary(captured: Path, name: str, goarch: str) -> Path:
    artifacts = json.loads((captured / "dist/artifacts.json").read_text())
    matches = [
        entry
        for entry in artifacts
        if entry.get("name") == name
        and entry.get("goos") == platform.system().lower()
        and entry.get("goarch") == goarch
        and entry.get("extra", {}).get("Format") == "binary"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"GoReleaser must produce exactly one {name} release artifact")
    binary = (captured / matches[0]["path"]).resolve()
    if not binary.is_relative_to((captured / "dist").resolve()) or not binary.is_file():
        raise RuntimeError("GoReleaser artifact escaped its output directory")
    return binary


def validate_artifact(
    binary: Path,
    machine: str,
    host_architecture: str,
    snapshot: str,
) -> dict[str, object]:
    data = binary.read_bytes()
    validate_executable(data, machine)
    if platform.system() == "Linux":
        return validate_linux_artifact(binary, machine, host_architecture, snapshot)
    expected = f"v{snapshot}"
    execution_status = "native" if machine == host_architecture else "Rosetta"
    if host_architecture == "x86_64" and machine == "arm64":
        if expected.encode() not in data:
            raise NativeAssetError("Cross-built arm64 image lacks the injected version")
        execution_status = "pending-native-arm64"
    else:
        command = [str(binary)]
        if machine != host_architecture:
            command = ["/usr/bin/arch", "-" + machine, str(binary)]
        try:
            version = tool(*command, "version")
        except (OSError, subprocess.SubprocessError) as error:
            raise RuntimeError(
                f"Cannot execute {machine} validation; install Rosetta on arm64 Macs"
            ) from error
        if version != expected:
            raise NativeAssetError("GoReleaser produced an unexpected Traforo version")

    libraries = sorted(
        line.strip().split(" (", 1)[0]
        for line in tool("/usr/bin/otool", "-L", str(binary)).splitlines()[1:]
    )
    if not set(libraries) <= LIBRARIES or "/usr/lib/libSystem.B.dylib" not in libraries:
        raise NativeAssetError("Local Traforo links an unexpected runtime library")
    return {
        "name": binary.name,
        "architecture": machine,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "version": expected,
        "direct_libraries": libraries,
        "execution": execution_status,
    }


def validate_linux_artifact(binary: Path, machine: str, host: str, snapshot: str) -> dict:
    data = binary.read_bytes()
    expected = f"v{snapshot}"
    execution = "pending-native-" + machine
    if machine == host:
        if tool(str(binary), "version") != expected:
            raise NativeAssetError("Linux image reports an unexpected version")
        execution = "native"
    elif expected.encode() not in data:
        raise NativeAssetError("Cross-built Linux image lacks the injected version")
    return {
        "name": binary.name,
        "architecture": machine,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "version": expected,
        "direct_libraries": [],
        "execution": execution,
    }


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _capture_source(source: Path, names: tuple[str, ...], source_hash: str, stage: Path) -> Path:
    captured = stage / "source"
    for name in names:
        target = captured / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    if inventory(captured) != names or fingerprint(captured, names) != source_hash:
        raise RuntimeError("Traforo source changed while capturing it; retry")
    return captured


def _release_environment(captured: Path, git_dir: str, snapshot: str) -> dict[str, str]:
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "TMPDIR",
            "GOCACHE",
            "GOMODCACHE",
            "GOPROXY",
            "GOTOOLCHAIN",
        )
        if key in os.environ
    }
    environment.update(
        GOWORK="off",
        GOENV="off",
        GIT_DIR=git_dir,
        GIT_OPTIONAL_LOCKS="0",
        GIT_WORK_TREE=str(captured),
        GORELEASER_CURRENT_TAG=f"v{TRAFORO_VERSION}",
        TRAFORO_SNAPSHOT_VERSION=snapshot,
    )
    if platform.system() == "Darwin":
        environment["SDKROOT"] = tool("/usr/bin/xcrun", "--show-sdk-path")
    return environment


def _run_release(
    releaser: str,
    captured: Path,
    environment: dict[str, str],
    snapshot: str,
    commit: str,
) -> tuple[list[str], dict[str, object]]:
    command = [releaser, "release", "--snapshot", "--clean", "--skip=publish,announce"]
    if platform.system() == "Linux":
        command += ["--config", ".goreleaser-linux.yml"]
    subprocess.run(  # noqa: S603 - captured source, no publish or host install
        command,
        cwd=captured,
        env=environment,
        check=True,
        timeout=900,
    )
    release_metadata = json.loads((captured / "dist/metadata.json").read_text())
    if release_metadata.get("version") != snapshot or release_metadata.get("commit") != commit:
        raise RuntimeError("GoReleaser metadata differs from the captured version/commit")
    return command, release_metadata


def _validate_bundle(
    captured: Path,
    bundle: Path,
    architecture: str,
    snapshot: str,
) -> list[dict[str, object]]:
    bundle.mkdir()
    records: list[dict[str, object]] = []
    for machine, goarch in (("arm64", "arm64"), ("x86_64", "amd64")):
        name = f"traforo-{platform.system().lower()}-{goarch}"
        binary = release_binary(captured, name, goarch)
        record = validate_artifact(binary, machine, architecture, snapshot)
        record["name"] = name
        target = bundle / name
        shutil.copyfile(binary, target)
        target.chmod(0o755)
        _fsync_file(target)
        records.append(record)
    return records


def _source_unchanged(source: Path, names: tuple[str, ...], source_hash: str, commit: str) -> None:
    if inventory(source) != names or fingerprint(source, names) != source_hash:
        raise RuntimeError("Traforo source changed during compilation; retry")
    if tool("git", "rev-parse", "HEAD", cwd=source) != commit:
        raise RuntimeError("Checkout commit changed during compilation; retry")


def _publish_bundle(
    output: Path,
    stage: Path,
    bundle: Path,
    provenance: dict[str, object],
) -> Path:
    record_path = bundle / "build.json"
    record_path.write_text(json.dumps(provenance, indent=2) + "\n")
    _fsync_file(record_path)
    _fsync_directory(bundle)
    destination = output / stage.name.removeprefix(".")
    bundle.replace(destination)
    _fsync_directory(output)
    return destination


def build(source: Path) -> Path:
    """Build both release images, validate them, and publish one bundle atomically."""
    source = source.resolve(strict=True)
    compiler, releaser, required_go, required = build_tools(source)
    architecture = _architecture()
    if architecture not in {"arm64", "x86_64"}:
        raise NativeAssetError("Traforo builds support macOS arm64 and x86_64")
    commit = tool("git", "rev-parse", "HEAD", cwd=source)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Cannot determine the checkout commit")
    git_dir = tool("git", "rev-parse", "--absolute-git-dir", cwd=source)
    snapshot = f"{TRAFORO_VERSION}-dev+g{commit[:12]}"
    names = inventory(source)
    source_hash = fingerprint(source, names)
    output = source / "build"
    if output.is_symlink():
        raise RuntimeError("Traforo build output directory must not be a symlink")
    output.mkdir(mode=0o700, exist_ok=True)
    output_info = output.lstat()
    if (
        not stat.S_ISDIR(output_info.st_mode)
        or output_info.st_uid != os.geteuid()
        or output_info.st_mode & 0o022
    ):
        raise RuntimeError("Traforo build output directory has unsafe ownership or permissions")

    with tempfile.TemporaryDirectory(prefix=".traforo-", dir=output) as temporary:
        stage = Path(temporary)
        captured = _capture_source(source, names, source_hash, stage)
        environment = _release_environment(captured, git_dir, snapshot)
        command, release_metadata = _run_release(releaser, captured, environment, snapshot, commit)
        bundle = stage / "bundle"
        records = _validate_bundle(captured, bundle, architecture, snapshot)
        _source_unchanged(source, names, source_hash, commit)
        provenance: dict[str, object] = {
            "source_sha256": source_hash,
            "commit": commit,
            "version": f"v{snapshot}",
            "minimum_macos": "15.0" if platform.system() == "Darwin" else None,
            "go_version": required_go,
            "goreleaser": required,
            "command": command[1:],
            "toolchain": tool(compiler, "version", cwd=captured, environment=environment),
            "sdk": tool("/usr/bin/xcrun", "--show-sdk-version")
            if platform.system() == "Darwin"
            else None,
            "clang": tool("/usr/bin/xcrun", "clang", "--version").splitlines()[0]
            if platform.system() == "Darwin"
            else None,
            "os": platform.system().lower(),
            "artifacts": records,
            "release_metadata": release_metadata,
        }
        destination = _publish_bundle(output, stage, bundle, provenance)

    goarch = "arm64" if architecture == "arm64" else "amd64"
    return destination / f"traforo-{platform.system().lower()}-{goarch}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-only",
        action="store_true",
        help="validate both images without installing the helper",
    )
    options = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1]

    import stelvio  # noqa: PLC0415 - verify this checkout's editable Python install

    expected_package = source.parents[1] / "stelvio"
    if Path(stelvio.__file__).resolve().parent != expected_package:
        raise RuntimeError(
            "Run with this Stelvio checkout's development environment: uv run python "
            "pkg/traforo-client-go/scripts/build.py"
        )

    binary = build(source)
    environment = dict(os.environ, **{DEVELOPMENT_BINARY_ENV: str(binary)})
    selected = select_traforo(environment)
    if not options.build_only:
        print(
            f"Installing local Traforo {selected.digest[:12]} "
            "(administrator authorization may be required).",
            flush=True,
        )
        install_helper(environment=environment)
        print("Local Traforo installed. Before running stlv in this shell, run:")
    else:
        print("Local Traforo built and validated. To select this image, run:")
    print(f"export {DEVELOPMENT_BINARY_ENV}={shlex.quote(str(binary))}")
    print("To return to the pinned release: unset STLV_TRAFORO_DEV_BINARY; stlv tunnel install")


if __name__ == "__main__":
    main()
