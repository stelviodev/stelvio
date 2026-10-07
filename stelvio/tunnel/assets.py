"""Validate packaged native assets before offering privileged installation."""

from __future__ import annotations

import hashlib
import json
import re
import struct
from contextlib import contextmanager
from importlib.resources import as_file, files
from typing import TYPE_CHECKING

from stelvio.tunnel.native.build import ASSET, LIBRARIES, SOURCES

if TYPE_CHECKING:
    from collections.abc import Iterator
    from importlib.resources.abc import Traversable
    from pathlib import Path

MAX_HELPER_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 16384
MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_HEADERS = 64
MACHO_HEADER = struct.Struct("<8I")
MACHO_ARM64 = 0x0100000C
MACHO_EXECUTABLE = 2
MACHO_MAGIC = 0xFEEDFACF
_HASH = re.compile(r"[0-9a-f]{64}")


class NativeAssetError(RuntimeError):
    """A native artifact is incomplete, incompatible, or stale."""


def _read(resource: Traversable, limit: int) -> bytes:
    with resource.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise NativeAssetError("The native helper bundle exceeds its size bounds")
    return data


def _manifest(raw: bytes) -> dict:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise NativeAssetError("The native helper manifest exceeds its size limit")
    try:
        value = json.loads(raw)
    except (UnicodeError, ValueError) as error:
        raise NativeAssetError("The native helper manifest is malformed") from error
    if not isinstance(value, dict):
        raise NativeAssetError("The native helper manifest is malformed")
    for key in ("format", "helper_abi", "carrier_version"):
        if type(value.get(key)) is not int or value[key] != 1:
            raise NativeAssetError("The native helper manifest is incompatible")
    if value.get("asset") != ASSET or value.get("platform") != "macos-arm64-darwin24":
        raise NativeAssetError("The native helper manifest names an unsupported artifact")
    libraries = value.get("direct_libraries")
    if not isinstance(libraries, list) or libraries != sorted(LIBRARIES):
        raise NativeAssetError("The native helper declares an unexpected runtime dependency")
    for key in ("sha256", "source_sha256"):
        if not isinstance(value.get(key), str) or not _HASH.fullmatch(value[key]):
            raise NativeAssetError("The native helper manifest has an invalid digest")
    return value


def _binary(data: bytes, expected: str) -> None:
    if not MACHO_HEADER.size <= len(data) <= MAX_HELPER_BYTES:
        raise NativeAssetError("The native helper artifact exceeds its size bounds")
    magic, cpu, _, kind, *_ = MACHO_HEADER.unpack_from(data)
    if magic != MACHO_MAGIC or cpu != MACHO_ARM64 or kind != MACHO_EXECUTABLE:
        raise NativeAssetError("The native helper is not an arm64 Mach-O executable")
    if hashlib.sha256(data).hexdigest() != expected:
        raise NativeAssetError("The native helper artifact does not match its manifest")


def _source_digest() -> str:
    source = files("stelvio.tunnel.native")
    headers = sorted(path.name for path in source.iterdir() if path.name.endswith(".h"))
    if len(headers) > MAX_HEADERS:
        raise NativeAssetError("The native helper source inventory exceeds its bound")
    digest = hashlib.sha256()
    for name in (*SOURCES, *headers):
        digest.update(name.encode("ascii") + b"\0")
        digest.update(_read(source.joinpath(name), MAX_SOURCE_BYTES))
        digest.update(b"\0")
    return digest.hexdigest()


@contextmanager
def packaged_helper() -> Iterator[Path]:
    """Yield a verified local asset while retaining any resource extraction."""
    assets = files("stelvio.tunnel").joinpath("_assets")
    try:
        manifest = _manifest(_read(assets.joinpath("manifest.json"), MAX_MANIFEST_BYTES))
        resource = assets.joinpath(ASSET)
        data = _read(resource, MAX_HELPER_BYTES)
        _binary(data, manifest["sha256"])
        if _source_digest() != manifest["source_sha256"]:
            raise NativeAssetError("The native helper artifact is stale for this package")
    except FileNotFoundError as error:
        raise NativeAssetError("The package is missing its native helper bundle") from error
    with as_file(resource) as path:
        # Resource extraction must preserve the bytes already validated above.
        _binary(_read(path, MAX_HELPER_BYTES), manifest["sha256"])
        yield path
