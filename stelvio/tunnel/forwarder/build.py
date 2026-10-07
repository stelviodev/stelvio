"""Nonroot release build, with source/artifact coherence checked before publish."""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from stelvio.tunnel.forwarding import ASSET, SOURCES


def fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for name in SOURCES:
        digest.update(name.encode() + b"\0" + (directory / name).read_bytes() + b"\0")
    return digest.hexdigest()


def build() -> None:
    if not os.geteuid() or platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("Build the nonroot forwarder on the macOS arm64 release host")
    source = Path(__file__).parent
    assets = source.parent / "_assets"
    with tempfile.TemporaryDirectory(prefix="forwarder-build-", dir=assets) as scratch:
        directory = Path(scratch)
        for name in SOURCES:
            (directory / name).write_bytes((source / name).read_bytes())
        digest = fingerprint(directory)
        binary = directory / ASSET
        compiler = shutil.which("go")
        if compiler is None:
            raise RuntimeError("Go release compiler is unavailable")
        subprocess.run(  # noqa: S603 - pinned Go module and captured owned release inputs
            [compiler, "build", "-mod=readonly", "-trimpath", "-o", str(binary), "."],
            cwd=directory,
            check=True,
            capture_output=True,
            timeout=120,
        )
        version = subprocess.run(  # noqa: S603 - own nonroot freshly built artifact
            [str(binary), "--version"],
            check=True,
            capture_output=True,
            timeout=5,
        )
        if version.stdout != b"stelvio-forwarder/1\n" or fingerprint(source) != digest:
            raise RuntimeError("Forwarder release inputs changed or version differs")
        manifest = {
            "format": 1,
            "carrier_version": 1,
            "asset": ASSET,
            "source_sha256": digest,
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        }
        metadata = directory / "forwarder.json"
        metadata.write_text(json.dumps(manifest, sort_keys=True) + "\n")
        binary.replace(assets / ASSET)
        metadata.replace(assets / "forwarder.json")


if __name__ == "__main__":
    build()
