"""Manual release builder; Python wheel installation never invokes a compiler."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

RELEASE_GO_VERSION = "go1.25.3"
RELEASE_SDK_VERSION = "15.5"
RELEASE_CLANG_VERSION = "Apple clang version 17.0.0 (clang-1700.0.13.5)"
MAX_BINARY_BYTES = 16 * 1024 * 1024
MACHO_MAGIC = 0xFEEDFACF
MACHO_EXECUTABLE = 2
VERSION = b"stelvio-traforo/1 helper=1 carrier=1\n"
LIBRARIES = frozenset(
    {
        "/usr/lib/libbsm.0.dylib",
        "/usr/lib/libSystem.B.dylib",
        "/usr/lib/libresolv.9.dylib",
        "/System/Library/Frameworks/SystemConfiguration.framework/Versions/A/SystemConfiguration",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
    }
)


def source_inventory(source: Path) -> tuple[str, ...]:
    return tuple(
        sorted(
            path.relative_to(source).as_posix()
            for path in source.rglob("*")
            if path.is_file()
            and (
                path.suffix in {".go", ".c", ".h"} or path.name in {"go.mod", "go.sum", "build.py"}
            )
        )
    )


def source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    for name in source_inventory(source):
        digest.update(name.encode("ascii") + b"\0" + (source / name).read_bytes() + b"\0")
    return digest.hexdigest()


def build() -> None:  # noqa: C901, PLR0915 - bounded two-architecture release transaction
    if platform.system() != "Darwin" or os.geteuid() == 0:
        raise RuntimeError("Build Traforo as an ordinary user on a macOS release host")
    compiler = shutil.which("go")
    if compiler is None:
        raise RuntimeError("The Go release compiler is unavailable")
    source = Path(__file__).resolve().parent
    assets = source.parent / "_assets"
    sdk = subprocess.check_output(["/usr/bin/xcrun", "--show-sdk-path"], text=True).strip()
    clang = subprocess.check_output(["/usr/bin/xcrun", "--find", "clang"], text=True).strip()
    toolchain = subprocess.check_output(  # noqa: S603 - nonroot build tool
        [compiler, "version"], text=True
    ).strip()
    if toolchain.split()[2] != RELEASE_GO_VERSION:
        raise RuntimeError(f"Build Traforo with the pinned {RELEASE_GO_VERSION} compiler")
    sdk_version = subprocess.check_output(
        ["/usr/bin/xcrun", "--show-sdk-version"], text=True
    ).strip()
    clang_version = subprocess.check_output([clang, "--version"], text=True).splitlines()[0]  # noqa: S603 - fixed SDK compiler
    if sdk_version != RELEASE_SDK_VERSION or clang_version != RELEASE_CLANG_VERSION:
        raise RuntimeError("Build Traforo with the pinned macOS SDK and Apple clang")
    fingerprint = source_digest(source)
    with tempfile.TemporaryDirectory(prefix="traforo-build-", dir=assets) as scratch:
        stage = Path(scratch)
        captured = stage / "source"
        captured.mkdir()
        inventory = source_inventory(source)
        for name in inventory:
            target = captured / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source / name).read_bytes())
        entries = {}
        for architecture, goarch in (("arm64", "arm64"), ("x86_64", "amd64")):
            asset = f"stelvio-traforo-macos-{architecture}"
            binary = stage / asset
            environment = {
                key: os.environ[key]
                for key in ("PATH", "HOME", "TMPDIR", "GOCACHE", "GOMODCACHE")
                if key in os.environ
            }
            environment.update(
                GOOS="darwin",
                GOARCH=goarch,
                CGO_ENABLED="1",
                GOTOOLCHAIN="local",
                GOWORK="off",
                GOENV="off",
                CC=f"{clang} -arch {architecture} -isysroot {sdk}",
                CGO_CFLAGS="-O2 -mmacosx-version-min=15.0",
                CGO_LDFLAGS="-mmacosx-version-min=15.0",
                MACOSX_DEPLOYMENT_TARGET="15.0",
            )
            subprocess.run(  # noqa: S603 - pinned module, captured release inputs
                [
                    compiler,
                    "build",
                    "-mod=readonly",
                    "-trimpath",
                    "-buildvcs=false",
                    "-ldflags=-s -w",
                    "-o",
                    str(binary),
                    ".",
                ],
                cwd=captured,
                env=environment,
                check=True,
                capture_output=True,
                timeout=300,
            )
            dependencies = subprocess.check_output(  # noqa: S603 - fixed system Mach-O inspector
                ["/usr/bin/otool", "-L", str(binary)], text=True
            )
            libraries = sorted(
                line.strip().split(" (", 1)[0] for line in dependencies.splitlines()[1:]
            )
            if not set(libraries) <= LIBRARIES or "/usr/lib/libSystem.B.dylib" not in libraries:
                raise RuntimeError("Traforo has unexpected runtime dependencies")
            data = binary.read_bytes()
            magic, cpu, _, kind = struct.unpack_from("<4I", data)
            expected_cpu = 0x0100000C if architecture == "arm64" else 0x01000007
            if magic != MACHO_MAGIC or cpu != expected_cpu or kind != MACHO_EXECUTABLE:
                raise RuntimeError("Traforo has an unexpected Mach-O architecture or image type")
            if len(data) > MAX_BINARY_BYTES:
                raise RuntimeError("Traforo exceeds its 16 MiB asset bound")
            entries[architecture] = {
                "asset": asset,
                "sha256": hashlib.sha256(data).hexdigest(),
                "bytes": len(data),
                "direct_libraries": libraries,
                "minimum_macos": "15.0",
                "architecture": architecture,
            }
        if source_digest(source) != fingerprint:
            raise RuntimeError("Traforo sources changed during compilation")
        native = "arm64" if platform.machine() == "arm64" else "x86_64"
        result = subprocess.run(  # noqa: S603 - freshly built ordinary-user artifact
            [str(stage / entries[native]["asset"]), "version"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        if result.stdout != VERSION or result.stderr:
            raise RuntimeError("Traforo version check failed")
        manifest = {
            "format": 2,
            "helper_abi": 1,
            "carrier_version": 1,
            "source_sha256": fingerprint,
            "source_inventory": inventory,
            "toolchain": toolchain,
            "sdk_version": sdk_version,
            "clang_version": clang_version,
            "build_flags": ["-mod=readonly", "-trimpath", "-buildvcs=false", "-ldflags=-s -w"],
            "targets": entries,
        }
        metadata = stage / "traforo.json"
        metadata.write_text(json.dumps(manifest, sort_keys=True) + "\n")
        for entry in entries.values():
            (stage / entry["asset"]).replace(assets / entry["asset"])
        metadata.replace(assets / "traforo.json")


if __name__ == "__main__":
    build()
