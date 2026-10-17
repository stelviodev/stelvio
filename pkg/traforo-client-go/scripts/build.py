# ruff: noqa: INP001, T201 - standalone monorepo developer tool, mirrored by the repo split.
"""Build this Mac's local Traforo source and install with explicit administrator approval."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path

from stelvio.tunnel.assets import (
    DEVELOPMENT_BINARY_ENV,
    NativeAssetError,
    select_traforo,
    validate_macho,
    validate_protocol,
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
            and "build" not in p.relative_to(source).parts
            and (p.suffix in {".go", ".c", ".h", ".json"} or p.name in {"go.mod", "go.sum"})
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


def build(source: Path) -> Path:
    """Capture source and publish only a fully validated host executable."""
    _supported()
    compiler = shutil.which("go")
    if compiler is None:
        raise RuntimeError("Install a Go toolchain compatible with go.mod before building Traforo")
    sdk = tool("/usr/bin/xcrun", "--show-sdk-path")
    clang = tool("/usr/bin/xcrun", "--find", "clang")
    architecture = platform.machine()
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
            GOOS="darwin",
            GOARCH="arm64" if architecture == "arm64" else "amd64",
            CGO_ENABLED="1",
            GOWORK="off",
            GOENV="off",
            CC=f"{shlex.quote(clang)} -arch {architecture} -isysroot {shlex.quote(sdk)}",
            CGO_CFLAGS="-O2 -mmacosx-version-min=15.0",
            CGO_LDFLAGS="-mmacosx-version-min=15.0",
            MACOSX_DEPLOYMENT_TARGET="15.0",
        )
        binary = stage / "stelvio-traforo"
        flags = ["-mod=readonly", "-trimpath", "-buildvcs=false", "-ldflags=-s -w"]
        subprocess.run(  # noqa: S603 - captured developer source, never privileged
            [compiler, "build", *flags, "-o", str(binary), "./cmd/stelvio-traforo"],
            cwd=captured,
            env=environment,
            check=True,
            timeout=300,
        )
        validate_macho(binary.read_bytes(), architecture)
        validate_protocol(binary)
        libraries = sorted(
            line.strip().split(" (", 1)[0]
            for line in tool("/usr/bin/otool", "-L", str(binary)).splitlines()[1:]
        )
        if not set(libraries) <= LIBRARIES or "/usr/lib/libSystem.B.dylib" not in libraries:
            raise NativeAssetError("Local Traforo links an unexpected runtime library")
        if inventory(source) != names or fingerprint(source, names) != source_hash:
            raise RuntimeError("Traforo source changed during compilation; retry")
        metadata = dict(  # noqa: C408 - human-readable build provenance
            source_sha256=source_hash,
            sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            architecture=architecture,
            bytes=binary.stat().st_size,
            minimum_macos="15.0",
            toolchain=tool(compiler, "version", cwd=captured, environment=environment),
            sdk=tool("/usr/bin/xcrun", "--show-sdk-version"),
            clang=tool(clang, "--version").splitlines()[0],
            direct_libraries=libraries,
            flags=flags,
        )
        binary.chmod(0o755)
        with binary.open("rb") as image:
            os.fsync(image.fileno())
        destination = output / f"stelvio-traforo-{architecture}"
        binary.replace(destination)
        record = stage / "build.json"
        record.write_text(json.dumps(metadata, indent=2) + "\n")
        record.replace(output / f"stelvio-traforo-{architecture}.json")
        return destination


def main(argv: list[str] | None = None) -> None:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
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
    print(
        f"Installing local Traforo {selected.digest[:12]} (macOS authorization may be required).",
        flush=True,
    )
    install_helper(environment=environment)
    print("Local Traforo installed. Before running stlv in this shell, run:")
    print(f"export {DEVELOPMENT_BINARY_ENV}={shlex.quote(str(binary))}")
    print("To return to the pinned release: unset STLV_TRAFORO_DEV_BINARY; stlv tunnel install")


if __name__ == "__main__":
    main()
