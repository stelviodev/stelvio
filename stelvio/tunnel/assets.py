"""Download and verify pinned Traforo releases into Stelvio's private binary cache."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import struct
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

import requests

from stelvio.pulumi import get_bin_path

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

DEVELOPMENT_BINARY_ENV = "STLV_TRAFORO_DEV_BINARY"
TRAFORO_VERSION = "0.0.5"
EXPECTED_VERSION = "stelvio-traforo/1 helper=1 carrier=1\n"
EXECUTABLE_MODE = 0o755
MAX_BINARY_BYTES = 16 * 1024 * 1024
MACHO_HEADER = struct.Struct("<8I")
MACHO_CPUS = {"arm64": 0x0100000C, "x86_64": 0x01000007}
# Release URLs and asset IDs describe the same immutable, digest-pinned artifacts.
TARGETS = {
    "arm64": {
        "url": "https://github.com/stelviodev/traforo/releases/download/v0.0.5/traforo-darwin-arm64",
        "api_url": "https://api.github.com/repos/stelviodev/traforo/releases/assets/651628204",
        "sha256": "b408afcaca01f760f4999c674a21271302c98eedad61678067ab80f844a4ba0f",
    },
    "x86_64": {
        "url": "https://github.com/stelviodev/traforo/releases/download/v0.0.5/traforo-darwin-amd64",
        "api_url": "https://api.github.com/repos/stelviodev/traforo/releases/assets/651628205",
        "sha256": "4008c435efb5bb3913784a10da61df8fdbaadcbc324e42f2bfd470dbbc86faba",
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


def validate_macho(data: bytes, architecture: str) -> None:
    """Validate structure independently of release or developer image identity."""
    if not MACHO_HEADER.size <= len(data) <= MAX_BINARY_BYTES:
        raise NativeAssetError("Traforo executable exceeds its size bounds")
    magic, cpu, _, kind, *_ = MACHO_HEADER.unpack_from(data)
    if (magic, cpu, kind) != (0xFEEDFACF, MACHO_CPUS[architecture], 2):
        raise NativeAssetError("Traforo is not a matching Mach-O executable")


def _binary(data: bytes, architecture: str) -> None:
    validate_macho(data, architecture)
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
    validate_protocol(path)
    return True


def validate_protocol(path: Path) -> None:
    """Run the ordinary-user version command after validating image bytes."""
    try:
        result = subprocess.run(  # noqa: S603 - validated ordinary-user executable
            [str(path), "version"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise NativeAssetError("Traforo version check failed") from None
    if result.returncode or result.stdout != EXPECTED_VERSION.encode() or result.stderr:
        raise NativeAssetError("Traforo reported an incompatible protocol version")


@dataclass(frozen=True)
class TraforoSelection:
    digest: str
    local_path: Path | None = None

    @property
    def notice(self) -> str | None:
        if self.local_path is not None:
            return f"Using developer Traforo {self.digest[:12]} from {self.local_path}"
        return None


def _developer_permissions(info: os.stat_result) -> None:
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or info.st_nlink != 1
        or info.st_mode & 0o7022
        or not info.st_mode & stat.S_IXUSR
    ):
        raise NativeAssetError("Developer Traforo has unsafe ownership or permissions")


def _no_developer_acl(path: Path) -> None:
    # Darwin ACL grants are independent of POSIX mode bits. Conservatively refuse
    # all ACLs, matching the privileged bootstrap's trusted-image boundary.
    try:
        output = subprocess.check_output(  # noqa: S603 - fixed system metadata inspector
            ["/bin/ls", "-lde", str(path)], stderr=subprocess.PIPE, timeout=5
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise NativeAssetError("Cannot verify developer Traforo ACLs") from None
    lines = output.splitlines()
    if len(lines) != 1 or b"+" in lines[0].split(maxsplit=1)[0]:
        raise NativeAssetError("Developer Traforo has unsafe extended ACLs")


def select_traforo(environment: Mapping[str, str] | None = None) -> TraforoSelection:
    """Explicit developer opt-in; otherwise select the immutable release pin."""
    architecture = _architecture()
    value = (os.environ if environment is None else environment).get(DEVELOPMENT_BINARY_ENV)
    if not value:
        return TraforoSelection(expected_digest())
    path = Path(value)
    if not path.is_absolute():
        raise NativeAssetError(f"{DEVELOPMENT_BINARY_ENV} must be an absolute executable path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            _developer_permissions(info)
            _no_developer_acl(path)
            data = stream.read(MAX_BINARY_BYTES + 1)
            validate_macho(data, architecture)
            digest = hashlib.sha256(data).hexdigest()
            validate_protocol(path)
            _no_developer_acl(path)
            after = path.lstat()
            _developer_permissions(after)
            stream.seek(0)
            if (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino) or hashlib.sha256(
                stream.read(MAX_BINARY_BYTES + 1)
            ).hexdigest() != digest:
                raise NativeAssetError(
                    "Developer Traforo changed during validation; rebuild and retry"
                )
    except OSError:
        raise NativeAssetError(
            f"Cannot read {DEVELOPMENT_BINARY_ENV}; rebuild or unset it to use the pinned release"
        ) from None
    return TraforoSelection(digest, path)


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


def validate_network_capabilities(path: Path) -> None:
    """Check a previously verified release, developer, or root-owned image."""
    instruction = (
        "Managed networking requires Traforo forwarder=2 and dns=1. "
        "Run stlv tunnel install to restore the pinned release; "
        "if STLV_TRAFORO_DEV_BINARY is set, rebuild that image or unset the override."
    )
    try:
        result = subprocess.run(  # noqa: S603 - previously validated executable
            [str(path), "capabilities"], capture_output=True, timeout=5, check=False
        )
        capabilities = json.loads(result.stdout)
        if (
            result.returncode
            or result.stderr
            or capabilities != {"forwarder": 2, "dns": 1}
            or any(type(value) is not int for value in capabilities.values())
        ):
            raise ValueError("capabilities")  # noqa: TRY301 - unify capability errors
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise NativeAssetError(instruction) from None


def require_network_capabilities(environment: Mapping[str, str] | None = None) -> None:
    """Gate managed startup before installation or any host mutation."""
    selected = select_traforo(environment)
    path = selected.local_path if selected.local_path is not None else ensure_traforo()
    validate_network_capabilities(path)
    if selected.local_path is not None:
        if select_traforo(environment).digest != selected.digest:
            raise NativeAssetError("Developer Traforo changed during capability validation")
    elif not _ready(path, _architecture()):
        raise NativeAssetError("Pinned Traforo disappeared during capability validation")
