# ruff: noqa: INP001, T201 - standalone monorepo developer tool, mirrored by the repo split.
"""Build this Mac's local Traforo source and install with explicit administrator approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from stelvio.tunnel.assets import (
    DEVELOPMENT_BINARY_ENV,
    TRAFORO_VERSION,
    NativeAssetError,
    select_traforo,
    validate_macho,
)
from stelvio.tunnel.installation import _supported, install_helper

LIBRARIES = frozenset(
    {
        "/usr/lib/libbsm.0.dylib",
        "/usr/lib/libSystem.B.dylib",
        "/usr/lib/libresolv.9.dylib",
        "/System/Library/Frameworks/SystemConfiguration.framework/Versions/A/SystemConfiguration",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
        # miekg/dns links Go crypto/x509, whose Darwin backend uses Security.
        "/System/Library/Frameworks/Security.framework/Versions/A/Security",
    }
)


def inventory(source: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            str(p.relative_to(source))
            for p in source.rglob("*")
            if p.is_file()
            and not {"build", "dist", ".git"}.intersection(p.relative_to(source).parts)
            and (
                p.suffix in {".go", ".c", ".h", ".json"}
                or p.name
                in {"go.mod", "go.sum", ".goreleaser.yml", "release.yml", "Makefile", "build.py"}
            )
        )
    )


def fingerprint(source: Path, names: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b"\0" + (source / name).read_bytes() + b"\0")
    return digest.hexdigest()


def tool(
    *arguments: str, cwd: Path | None = None, environment: dict[str, str] | None = None
) -> str:
    return subprocess.check_output(  # noqa: S603 - developer build tools
        arguments, cwd=cwd, env=environment, text=True, timeout=60
    ).strip()


def goreleaser_version(source: Path) -> str:
    workflow = (source / ".github/workflows/release.yml").read_text()
    match = re.search(r"version: (v[0-9]+\.[0-9]+\.[0-9]+)\s*$", workflow, re.MULTILINE)
    if not match:
        raise RuntimeError("Cannot find the pinned GoReleaser version in release.yml")
    return match[1]


def validate_artifact(binary: Path, machine: str, architecture: str, snapshot: str) -> dict:
    data = binary.read_bytes()
    validate_macho(data, machine)
    expected = f"v{snapshot}"
    execution_status = "native" if machine == architecture else "Rosetta"
    if architecture == "x86_64" and machine == "arm64":
        # Intel cannot execute arm64. The release metadata is checked separately;
        # require the linker-injected snapshot string in this image as well.
        if expected.encode() not in data:
            raise NativeAssetError("Cross-built arm64 image lacks the injected version")
        version = expected
        execution_status = "pending-native-arm64"
    else:
        execution = [str(binary)]
        if machine != architecture:
            execution = ["/usr/bin/arch", "-" + machine, str(binary)]
        try:
            version = tool(*execution, "version")
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
    return dict(  # noqa: C408 - human-readable provenance
        name=binary.name,
        architecture=machine,
        sha256=hashlib.sha256(data).hexdigest(),
        bytes=len(data),
        version=version,
        direct_libraries=libraries,
        execution=execution_status,
    )


def build_tools(source: Path) -> tuple[str, str, str]:
    """Require the release toolchain before creating build output."""
    _supported()
    compiler = shutil.which("go")
    releaser = shutil.which("goreleaser")
    required = goreleaser_version(source)
    if compiler is None or releaser is None:
        raise RuntimeError(f"Install Go compatible with go.mod and GoReleaser {required}")
    reported = tool(releaser, "--version")
    if not re.search(
        r"(?m)^GitVersion:\s*" + re.escape(required.removeprefix("v")) + r"\s*$", reported
    ):
        raise RuntimeError(f"Install GoReleaser {required}, matching release.yml")
    return compiler, releaser, required


def release_binary(captured: Path, name: str, goarch: str) -> Path:
    artifacts = json.loads((captured / "dist/artifacts.json").read_text())
    matches = [
        entry
        for entry in artifacts
        if entry.get("name") == name
        and entry.get("goos") == "darwin"
        and entry.get("goarch") == goarch
        and entry.get("extra", {}).get("Format") == "binary"
    ]
    if len(matches) != 1:
        raise RuntimeError(f"GoReleaser must produce exactly one {name} release artifact")
    binary = (captured / matches[0]["path"]).resolve()
    if not binary.is_relative_to((captured / "dist").resolve()):
        raise RuntimeError("GoReleaser artifact escaped its output directory")
    return binary


def build(source: Path) -> Path:
    """Use the release configuration, validate both images, then publish a bundle."""
    compiler, releaser, required = build_tools(source)
    architecture = platform.machine()
    commit = tool("git", "rev-parse", "HEAD", cwd=source)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Cannot determine the checkout commit")
    git_dir = tool("git", "rev-parse", "--absolute-git-dir", cwd=source)
    snapshot = f"{TRAFORO_VERSION}-dev+g{commit[:12]}"
    names = inventory(source)
    source_hash = fingerprint(source, names)
    output = source / "build"
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".traforo-", dir=output) as scratch:
        stage = Path(scratch)
        captured = stage / "source"
        for name in names:
            target = captured / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        if fingerprint(captured, names) != source_hash:
            raise RuntimeError("Traforo source changed while capturing it; retry")
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
            SDKROOT=tool("/usr/bin/xcrun", "--show-sdk-path"),
        )
        command = [
            releaser,
            "release",
            "--snapshot",
            "--clean",
            "--skip=publish,announce",
        ]
        subprocess.run(  # noqa: S603 - captured source; publication disabled, never privileged
            command,
            cwd=captured,
            env=environment,
            check=True,
            timeout=900,
        )
        release_metadata = json.loads((captured / "dist/metadata.json").read_text())
        if release_metadata.get("version") != snapshot or release_metadata.get("commit") != commit:
            raise RuntimeError("GoReleaser metadata differs from the captured version/commit")
        bundle = stage / "bundle"
        bundle.mkdir()
        records = []
        for machine, goarch in (("arm64", "arm64"), ("x86_64", "amd64")):
            name = f"traforo-darwin-{goarch}"
            binary = release_binary(captured, name, goarch)
            record = dict(validate_artifact(binary, machine, architecture, snapshot), name=name)
            target = bundle / name
            shutil.copyfile(binary, target)
            target.chmod(0o755)
            with target.open("rb") as image:
                os.fsync(image.fileno())
            records.append(record)
        if inventory(source) != names or fingerprint(source, names) != source_hash:
            raise RuntimeError("Traforo source changed during compilation; retry")
        if tool("git", "rev-parse", "HEAD", cwd=source) != commit:
            raise RuntimeError("Checkout commit changed during compilation; retry")
        metadata = dict(  # noqa: C408 - human-readable build provenance
            source_sha256=source_hash,
            commit=commit,
            version=f"v{snapshot}",
            minimum_macos="15.0",
            goreleaser=required,
            command=command[1:],
            toolchain=tool(compiler, "version", cwd=captured, environment=environment),
            sdk=tool("/usr/bin/xcrun", "--show-sdk-version"),
            clang=tool("/usr/bin/xcrun", "clang", "--version").splitlines()[0],
            artifacts=records,
            release_metadata=release_metadata,
        )
        record = bundle / "build.json"
        record.write_text(json.dumps(metadata, indent=2) + "\n")
        with record.open("rb") as stream:
            os.fsync(stream.fileno())
        # A unique directory publishes both images and provenance in one rename.
        destination = output / stage.name.removeprefix(".")
        bundle.replace(destination)
        return destination / f"traforo-darwin-{'arm64' if architecture == 'arm64' else 'amd64'}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--build-only", action="store_true", help="Validate both images without installing"
    )
    options = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1]
    import stelvio  # noqa: PLC0415 - verify this checkout's editable Python installation

    if Path(stelvio.__file__).resolve().parent != source.parents[1] / "stelvio":
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
            "(macOS authorization may be required).",
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
