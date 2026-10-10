"""Select and verify precompiled Traforo assets; never compile at installation."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import struct
from contextlib import contextmanager
from importlib.resources import as_file, files
from typing import TYPE_CHECKING

from stelvio.tunnel.traforo.build import LIBRARIES, MAX_BINARY_BYTES

if TYPE_CHECKING:
    from collections.abc import Iterator
    from importlib.resources.abc import Traversable
    from pathlib import Path

MAX_HELPER_BYTES = MAX_BINARY_BYTES
MAX_MANIFEST_BYTES = 65536
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MACHO_MAGIC = 0xFEEDFACF
MACHO_EXECUTABLE = 2
MAX_SOURCE_FILES = 512
MACHO_HEADER = struct.Struct("<8I")
MACHO_CPUS = {"arm64": 0x0100000C, "x86_64": 0x01000007}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class NativeAssetError(RuntimeError):
    """A Traforo artifact is incomplete, incompatible, or stale."""


def _read(resource: Traversable, limit: int) -> bytes:
    with resource.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise NativeAssetError("The Traforo bundle exceeds its size bounds")
    return data


def _architecture() -> str:
    machine = platform.machine()
    if platform.system() != "Darwin" or machine not in MACHO_CPUS:
        raise NativeAssetError("Traforo networking supports macOS arm64 and x86_64")
    return machine


def _manifest(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise NativeAssetError("The Traforo manifest is malformed") from error
    if not isinstance(value, dict):
        raise NativeAssetError("The Traforo manifest is malformed")
    for key, expected in (("format", 2), ("helper_abi", 1), ("carrier_version", 1)):
        if type(value.get(key)) is not int or value[key] != expected:
            raise NativeAssetError("The Traforo manifest is incompatible")
    if not isinstance(value.get("source_sha256"), str) or not _HASH.fullmatch(
        value["source_sha256"]
    ):
        raise NativeAssetError("The Traforo manifest has an invalid source digest")
    targets = value.get("targets")
    if not isinstance(targets, dict) or set(targets) != set(MACHO_CPUS):
        raise NativeAssetError("The Traforo manifest has an incomplete architecture inventory")
    for architecture, entry in targets.items():
        if (
            not isinstance(entry, dict)
            or entry.get("asset") != f"stelvio-traforo-macos-{architecture}"
            or entry.get("architecture") != architecture
            or entry.get("minimum_macos") != "15.0"
            or not isinstance(entry.get("sha256"), str)
            or not _HASH.fullmatch(entry["sha256"])
            or type(entry.get("bytes")) is not int
            or not MACHO_HEADER.size <= entry["bytes"] <= MAX_BINARY_BYTES
        ):
            raise NativeAssetError("The Traforo target manifest is incompatible")
        libraries = entry.get("direct_libraries")
        if (
            not isinstance(libraries, list)
            or not all(isinstance(item, str) for item in libraries)
            or libraries != sorted(set(libraries))
            or not set(libraries) <= LIBRARIES
            or "/usr/lib/libSystem.B.dylib" not in libraries
        ):
            raise NativeAssetError("Traforo declares an unexpected runtime dependency")
    return value


def _binary(data: bytes, entry: dict) -> None:
    if not MACHO_HEADER.size <= len(data) <= MAX_BINARY_BYTES or len(data) != entry["bytes"]:
        raise NativeAssetError("The Traforo artifact exceeds its size bounds or declared size")
    magic, cpu, _, kind, *_ = MACHO_HEADER.unpack_from(data)
    if (
        magic != MACHO_MAGIC
        or cpu != MACHO_CPUS[entry["architecture"]]
        or kind != MACHO_EXECUTABLE
    ):
        raise NativeAssetError("Traforo is not a matching Mach-O executable")
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise NativeAssetError("The Traforo artifact does not match its manifest")


def _source_inventory(source: Traversable, prefix: str = "") -> list[str]:
    names = []
    for path in source.iterdir():
        name = prefix + path.name
        if path.is_dir() and path.name != "__pycache__":
            names.extend(_source_inventory(path, name + "/"))
        elif path.is_file() and (
            path.name.endswith((".go", ".c", ".h"))
            or path.name in {"go.mod", "go.sum", "build.py"}
        ):
            names.append(name)
    if len(names) > MAX_SOURCE_FILES:
        raise NativeAssetError("The Traforo source inventory exceeds its bound")
    return sorted(names)


def _source_digest(manifest: dict) -> str:
    source = files("stelvio.tunnel.traforo")
    inventory = _source_inventory(source)
    if manifest.get("source_inventory") != inventory:
        raise NativeAssetError("The Traforo source inventory is stale")
    digest = hashlib.sha256()
    for name in inventory:
        digest.update(name.encode("ascii") + b"\0")
        digest.update(_read(source.joinpath(name), MAX_SOURCE_BYTES))
        digest.update(b"\0")
    return digest.hexdigest()


@contextmanager
def packaged_helper() -> Iterator[Path]:
    """Yield the host architecture's verified, precompiled Traforo asset."""
    architecture = _architecture()
    assets = files("stelvio.tunnel").joinpath("_assets")
    try:
        manifest = _manifest(_read(assets.joinpath("traforo.json"), MAX_MANIFEST_BYTES))
        entry = manifest["targets"][architecture]
        resource = assets.joinpath(entry["asset"])
        _binary(_read(resource, MAX_BINARY_BYTES), entry)
        if _source_digest(manifest) != manifest["source_sha256"]:
            raise NativeAssetError("The Traforo artifact is stale for this package")
    except FileNotFoundError as error:
        raise NativeAssetError("The package is missing its Traforo bundle") from error
    with as_file(resource) as path:
        _binary(_read(path, MAX_BINARY_BYTES), entry)
        yield path
