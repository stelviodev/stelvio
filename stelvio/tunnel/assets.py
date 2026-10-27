"""Download and verify pinned Traforo releases into Stelvio's private binary cache."""

from __future__ import annotations

import hashlib
import os
import platform
import re
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
TRAFORO_VERSION = "0.0.7"
EXPECTED_VERSION = f"v{TRAFORO_VERSION}\n"
EXECUTABLE_MODE = 0o755
LOCK_MODE = 0o600
MAX_BINARY_BYTES = 16 * 1024 * 1024
MACHO_HEADER = struct.Struct("<8I")
MACHO_CPUS = {"arm64": 0x0100000C, "x86_64": 0x01000007}
READ_ONLY_ACL_RIGHTS = {
    "read",
    "read_data",
    "readattr",
    "read_attr",
    "readextattr",
    "read_extattr",
    "readsync",
    "read_sync",
    "execute",
    "list",
    "search",
    "synchronize",
    "file_inherit",
    "directory_inherit",
    "only_inherit",
    "inherited",
    "limit_inherit",
    "no_inherit",
    "no_propagate",
}
MIN_ACL_FIELDS = 3
# Release URLs and asset IDs describe the same immutable, digest-pinned artifacts.
TARGETS = {
    "arm64": {
        "url": "https://github.com/stelviodev/traforo/releases/download/v0.0.7/traforo-darwin-arm64",
        "api_url": "https://api.github.com/repos/stelviodev/traforo/releases/assets/654424977",
        "sha256": "14327d6881b2c6d01356fd390ff1c0fd7d57e42fafc2874b5af82a7aafcb2da7",
    },
    "x86_64": {
        "url": "https://github.com/stelviodev/traforo/releases/download/v0.0.7/traforo-darwin-amd64",
        "api_url": "https://api.github.com/repos/stelviodev/traforo/releases/assets/654424978",
        "sha256": "a1adc747fc328e36e69ebc2e0e74fbb8a48e6d2eacc9403ae4a22b81bd6757bd",
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


def _validate_cache_directory(path: Path) -> None:
    """Require the per-user cache directory to remain a private owned directory."""
    try:
        info = path.lstat()
    except OSError:
        raise NativeAssetError("Stelvio binary cache is unavailable or unsafe") from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise NativeAssetError("Stelvio binary cache is unavailable or unsafe")
    resolved = _validate_private_directory_chain(path, "Stelvio binary cache")
    if resolved != path:
        raise NativeAssetError("Stelvio binary cache has an unsafe path")


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
    _no_acl(path, "Cached Traforo")
    with path.open("rb") as stream:
        _binary(stream.read(MAX_BINARY_BYTES + 1), architecture)
    validate_version(path)
    try:
        with path.open("rb") as stream:
            _binary(stream.read(MAX_BINARY_BYTES + 1), architecture)
    except OSError:
        raise NativeAssetError("Cached Traforo changed during version validation") from None
    return True


def validate_version(path: Path, *, allow_development: bool = False) -> str:
    """Validate the single binary version after validating executable bytes."""
    try:
        result = subprocess.run(  # noqa: S603 - validated ordinary-user executable
            [str(path), "version"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise NativeAssetError("Traforo version check failed") from None
    release = f"v{TRAFORO_VERSION}"
    if not result.returncode and not result.stderr:
        if result.stdout == (release + "\n").encode():
            return release
        if allow_development and re.fullmatch(
            re.escape(release).encode() + rb"-dev\+g[0-9a-f]{7,40}\n", result.stdout
        ):
            return result.stdout.decode().strip()
    raise NativeAssetError(
        f"Traforo must report {release}; use a matching release or developer image"
    )


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


def _no_acl(path: Path, label: str) -> None:  # noqa: C901 - ACL metadata parser
    """Reject Darwin ACL grants that can change or replace a verified image."""
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed system metadata inspector
            ["/bin/ls", "-lde", str(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise NativeAssetError(f"Cannot verify {label} ACLs") from None
        if process.returncode:
            raise NativeAssetError(f"Cannot verify {label} ACLs")
    except OSError:
        raise NativeAssetError(f"Cannot verify {label} ACLs") from None
    lines = output.splitlines()
    if not lines:
        raise NativeAssetError(f"Cannot verify {label} ACLs")
    header = lines[0].split(maxsplit=1)
    if not header:
        raise NativeAssetError(f"Cannot verify {label} ACLs")
    marker = header[0][-1:]
    # Darwin displays @ instead of + when extended attributes and ACLs coexist.
    # @ alone is also valid; any following ACL rows still need full validation.
    if marker not in {b"+", b"@"} and len(lines) != 1:
        raise NativeAssetError(f"{label} has unsafe extended ACLs")
    if marker == b"+" and len(lines) == 1:
        raise NativeAssetError(f"Cannot verify {label} ACLs")
    for line in lines[1:]:
        fields = line.decode(errors="replace").split()
        if len(fields) < MIN_ACL_FIELDS or not fields[0].endswith(":"):
            raise NativeAssetError(f"Cannot verify {label} ACLs")
        if "deny" in fields:
            continue
        try:
            allow_index = fields.index("allow")
        except ValueError:
            raise NativeAssetError(f"Cannot verify {label} ACLs") from None
        rights = {
            right.strip().lower()
            for field in fields[allow_index + 1 :]
            for right in field.split(",")
            if right.strip()
        }
        if not rights or not rights <= READ_ONLY_ACL_RIGHTS:
            raise NativeAssetError(f"{label} has unsafe extended ACLs")


def _no_developer_acl(path: Path) -> None:
    # Darwin ACL grants are independent of POSIX mode bits. Refuse write grants
    # for explicit developer images at the trusted-image boundary.
    _no_acl(path, "Developer Traforo")


def _validate_private_directory_chain(path: Path, label: str) -> Path:
    """Resolve and trust a private directory and every ancestor directory.

    Checking only the file or leaf directory leaves a replacement race when
    another principal can write a parent directory. Root-owned sticky
    directories such as /tmp are safe because another user cannot replace an
    entry owned by this user.
    """
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise NativeAssetError(f"Cannot verify {label} directories") from None
    directories = (resolved, *resolved.parents)
    euid = os.geteuid()
    for directory in directories:
        try:
            info = directory.lstat()
        except OSError:
            raise NativeAssetError(f"Cannot verify {label} directories") from None
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, euid}:
            raise NativeAssetError(f"{label} has an unsafe directory owner")
        shared_write = info.st_mode & 0o022
        root_sticky = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        if shared_write and not root_sticky:
            raise NativeAssetError(f"{label} has a writable directory")
        _no_acl(directory, label)
    return resolved


def _validate_developer_parent(path: Path) -> Path:
    """Trust the complete chain containing a local executable before use."""
    return _validate_private_directory_chain(path.parent, "Developer Traforo")


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
        parent = _validate_developer_parent(path)
        # Use the canonical, already-checked parent chain for both validation
        # and execution; do not retain an untrusted symlinked prefix.
        path = parent / path.name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            _developer_permissions(info)
            _no_developer_acl(path)
            data = stream.read(MAX_BINARY_BYTES + 1)
            validate_macho(data, architecture)
            digest = hashlib.sha256(data).hexdigest()
            validate_version(path, allow_development=True)
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
        raise NativeAssetError("Traforo download failed; check network access and retry") from None
    raise NativeAssetError("Traforo download exceeded its redirect limit")


def ensure_traforo() -> Path:
    """Verify cached image or download atomically; never compile or consult PATH."""
    architecture = _architecture()
    import fcntl  # noqa: PLC0415 - only after the platform guard

    path = traforo_path()
    _validate_cache_directory(path.parent)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path.parent / "traforo.install.lock", flags, 0o600)
    except OSError:
        raise NativeAssetError("Traforo installation lock is unsafe") from None
    with os.fdopen(fd, "rb+") as lock:
        info = os.fstat(lock.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != LOCK_MODE
        ):
            raise NativeAssetError("Traforo installation lock has unsafe ownership")
        _no_acl(path.parent / "traforo.install.lock", "Traforo installation lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if _ready(path, architecture):
                return path
        except NativeAssetError:
            try:
                info = path.lstat()
            except FileNotFoundError:
                raise NativeAssetError("Cached Traforo disappeared during validation") from None
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


def require_network_version(environment: Mapping[str, str] | None = None) -> None:
    """Gate managed startup before installation or any host mutation."""
    selected = select_traforo(environment)
    path = selected.local_path if selected.local_path is not None else ensure_traforo()
    validate_version(path, allow_development=selected.local_path is not None)
    if selected.local_path is not None:
        if select_traforo(environment).digest != selected.digest:
            raise NativeAssetError("Developer Traforo changed during version validation")
    elif not _ready(path, _architecture()):
        raise NativeAssetError("Pinned Traforo disappeared during version validation")
