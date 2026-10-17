"""Download and verify pinned Traforo releases into Stelvio's private binary cache."""

from __future__ import annotations

import hashlib
import os
import platform
import stat
import struct
import subprocess
import tempfile
from contextlib import contextmanager
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import requests

from stelvio.pulumi import get_bin_path

if TYPE_CHECKING:
    from collections.abc import Iterator

TRAFORO_VERSION = "0.0.4"
EXPECTED_VERSION = "stelvio-traforo/1 helper=1 carrier=1\n"
EXECUTABLE_MODE = 0o755
MAX_BINARY_BYTES = 16 * 1024 * 1024
MACHO_HEADER = struct.Struct("<8I")
MACHO_CPUS = {"arm64": 0x0100000C, "x86_64": 0x01000007}
# Release URLs and asset IDs describe the same immutable, digest-pinned artifacts.
TARGETS = {
    "arm64": {
        "url": "https://github.com/stelviodev/traforo/releases/download/v0.0.4/traforo-darwin-arm64",
        "api_url": "https://api.github.com/repos/stelviodev/traforo/releases/assets/648296580",
        "sha256": "96bebda932a84d2ec277ad60ada6f61db2c1525fc65247b3d17239f4ef696fcb",
    },
    "x86_64": {
        "url": "https://github.com/stelviodev/traforo/releases/download/v0.0.4/traforo-darwin-amd64",
        "api_url": "https://api.github.com/repos/stelviodev/traforo/releases/assets/648296588",
        "sha256": "dd0c8f07bfceb2dd5267cd7abfbd0762dbc6d5a905fb76b7e4d89fdd227895d2",
    },
}


class NativeAssetError(RuntimeError):
    """A Traforo download or cached artifact is unavailable or incompatible."""


def _architecture() -> str:
    machine = platform.machine()
    if platform.system() != "Darwin" or machine not in MACHO_CPUS:
        raise NativeAssetError("Traforo networking supports macOS arm64 and x86_64")
    return machine


def expected_digest() -> str:
    """Expected image identity, available without network or cache access."""
    return TARGETS[_architecture()]["sha256"]


def traforo_path() -> Path:
    architecture = _architecture()
    return get_bin_path() / f"stelvio-traforo-{TRAFORO_VERSION}-{architecture}"


def _binary(data: bytes, architecture: str) -> None:
    if not MACHO_HEADER.size <= len(data) <= MAX_BINARY_BYTES:
        raise NativeAssetError("Traforo executable exceeds its size bounds")
    magic, cpu, _, kind, *_ = MACHO_HEADER.unpack_from(data)
    if (magic, cpu, kind) != (0xFEEDFACF, MACHO_CPUS[architecture], 2):
        raise NativeAssetError("Traforo is not a matching Mach-O executable")
    if hashlib.sha256(data).hexdigest() != TARGETS[architecture]["sha256"]:
        raise NativeAssetError("Traforo executable digest does not match its release pin")


def _ready(path: Path, architecture: str) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or stat.S_IMODE(info.st_mode) != EXECUTABLE_MODE
    ):
        raise NativeAssetError("Cached Traforo has unsafe ownership or permissions")
    with path.open("rb") as stream:
        _binary(stream.read(MAX_BINARY_BYTES + 1), architecture)
    try:
        result = subprocess.run(  # noqa: S603 - digest-verified private executable
            [str(path), "version"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise NativeAssetError("Cached Traforo version check failed") from None
    if result.returncode or result.stdout != EXPECTED_VERSION.encode() or result.stderr:
        raise NativeAssetError("Cached Traforo reported an incompatible protocol version")
    return True


def _environment_auth(request: requests.PreparedRequest) -> requests.PreparedRequest:
    # An explicit auth callable suppresses Requests' implicit .netrc credentials.
    # Keep the Authorization header set only from this process's GITHUB_TOKEN.
    return request


def _download(architecture: str) -> bytes:  # noqa: C901 - bounded authenticated redirect loop
    target = TARGETS[architecture]
    token = os.environ.get("GITHUB_TOKEN")
    url = target["api_url"] if token else target["url"]
    headers = {"Accept": "application/octet-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        # Follow redirects explicitly: credentials belong only to the API origin.
        for _ in range(6):
            with requests.get(
                url,
                headers=headers,
                auth=_environment_auth,
                stream=True,
                timeout=(10, 60),
                allow_redirects=False,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise NativeAssetError("Traforo download returned an invalid redirect")
                    following = urljoin(url, location)
                    parsed = urlparse(following)
                    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                        raise NativeAssetError("Traforo download returned an unsafe redirect")
                    if urlparse(url).netloc != parsed.netloc:
                        headers = {"Accept": "application/octet-stream"}
                    url = following
                    continue
                if response.status_code != HTTPStatus.OK:
                    raise NativeAssetError(
                        f"Traforo download failed (HTTP {response.status_code}). "
                        "Private releases require GITHUB_TOKEN with repository read access; "
                        "public releases can be downloaded without a token."
                    )
                data = bytearray()
                for chunk in response.iter_content(65536):
                    data.extend(chunk)
                    if len(data) > MAX_BINARY_BYTES:
                        raise NativeAssetError("Traforo download exceeds its size bound")
                return bytes(data)
    except requests.RequestException:
        # Requests errors may contain signed CDN URLs; never propagate those details.
        raise NativeAssetError("Traforo download failed; retry stlv tunnel install") from None
    raise NativeAssetError("Traforo download exceeded its redirect limit")


def ensure_traforo() -> Path:
    """Verify cached image or download atomically; never compile or consult PATH."""
    architecture = _architecture()
    import fcntl  # noqa: PLC0415 - only after the platform guard

    path = traforo_path()
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.parent / "traforo.install.lock", flags, 0o600)
    with os.fdopen(fd, "rb+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise NativeAssetError("Traforo installation lock has unsafe ownership")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if _ready(path, architecture):
                return path
        except NativeAssetError:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise
        data = _download(architecture)
        _binary(data, architecture)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".traforo-", dir=path.parent, delete=False
            ) as stage:
                pending = Path(stage.name)
                stage.write(data)
                stage.flush()
                os.fchmod(stage.fileno(), EXECUTABLE_MODE)
                os.fsync(stage.fileno())
            _ready(pending, architecture)
            pending.replace(path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if pending is not None:
                pending.unlink(missing_ok=True)
        return path


@contextmanager
def downloaded_helper() -> Iterator[Path]:
    """Yield the verified private release image for helper installation."""
    yield ensure_traforo()
