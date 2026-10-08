"""Build the native helper asset on the declared macOS release host.

This is release tooling, never run by the privileged service or an installer.
Wheel and sdist creation must include its resulting asset and manifest together.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path

SOURCES = (
    "acl.c",
    "protocol.c",
    "io.c",
    "host.c",
    "ownership.c",
    "interface.c",
    "route_reply.c",
    "packet.c",
    "packet_io.c",
    "pump.c",
    "journal.c",
    "journal_format.c",
    "snapshot.c",
    "state_store.c",
    "resolver_plan.c",
    "resolver_files.c",
    "resolver_domains.c",
    "resolver_inventory.c",
    "unit.c",
    "service.c",
    "admin.c",
    "main.c",
)
LIBRARIES = frozenset(
    {
        "/usr/lib/libbsm.0.dylib",
        "/usr/lib/libSystem.B.dylib",
        "/System/Library/Frameworks/SystemConfiguration.framework/Versions/A/SystemConfiguration",
        "/System/Library/Frameworks/CoreFoundation.framework/Versions/A/CoreFoundation",
    }
)
ASSET = "helper-macos-arm64"
SDK = "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"


def source_digest(source: Path) -> str:
    digest = hashlib.sha256()
    names = (*SOURCES, *(path.name for path in sorted(source.glob("*.h"))))
    for name in names:
        digest.update(name.encode("ascii") + b"\0")
        digest.update((source / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def build() -> None:
    if not os.geteuid():
        raise RuntimeError("Native release tooling must run without root privileges")
    if (
        platform.system() != "Darwin"
        or platform.machine() != "arm64"
        or platform.release() != "24.6.0"
    ):
        raise RuntimeError("Build the helper on the declared Darwin24 arm64 release host")
    source = Path(__file__).resolve().parent
    assets = source.parent / "_assets"
    assets.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-build-", dir=assets) as scratch:
        temporary = Path(scratch)
        binary = temporary / ASSET
        captured = temporary / "sources"
        captured.mkdir()
        for name in (*SOURCES, *(path.name for path in sorted(source.glob("*.h")))):
            (captured / name).write_bytes((source / name).read_bytes())
        fingerprint = source_digest(captured)
        environment = {"PATH": "/usr/bin:/bin", "TMPDIR": str(temporary)}
        subprocess.run(  # noqa: S603 - fixed system compiler and bounded source inventory
            [
                "/usr/bin/clang",
                "-std=c17",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-isysroot",
                SDK,
                *(str(captured / name) for name in SOURCES),
                "-lbsm",
                "-framework",
                "SystemConfiguration",
                "-framework",
                "CoreFoundation",
                "-o",
                str(binary),
            ],
            check=True,
            capture_output=True,
            timeout=60,
            env=environment,
        )
        dependencies = subprocess.run(  # noqa: S603 - system read-only Mach-O inspection
            ["/usr/bin/otool", "-L", str(binary)],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env=environment,
        )
        found = frozenset(
            line.strip().split(" (", 1)[0] for line in dependencies.stdout.splitlines()[1:]
        )
        if found != LIBRARIES:
            raise RuntimeError(
                "Native helper runtime closure differs from the declared system libraries"
            )
        version = subprocess.run(  # noqa: S603 - own freshly compiled nonprivileged artifact
            [str(binary), "--version"],
            check=True,
            capture_output=True,
            timeout=5,
            env=environment,
        )
        if version.stdout != b"stelvio-tunnel-helper/1\n" or version.stderr:
            raise RuntimeError("Native helper version check failed")
        if source_digest(source) != fingerprint:
            raise RuntimeError(
                "Native sources changed during the release build; rebuild the asset"
            )
        manifest = {
            "format": 1,
            "helper_abi": 1,
            "carrier_version": 1,
            "platform": "macos-arm64-darwin24",
            "asset": ASSET,
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "source_sha256": fingerprint,
            "direct_libraries": sorted(found),
        }
        metadata = temporary / "manifest.json"
        metadata.write_text(json.dumps(manifest, sort_keys=True) + "\n")
        binary.replace(assets / ASSET)
        metadata.replace(assets / "manifest.json")


if __name__ == "__main__":
    build()
