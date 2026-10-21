"""Pinned, private AWS plugin installation. No PATH lookup or installer execution."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import struct
import subprocess
import tempfile
import zipfile
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from stelvio.pulumi import get_bin_path
from stelvio.tunnel.assets import (
    MACHO_CPUS,
    NativeAssetError,
    _architecture,
    _environment_auth,
    _no_acl,
    _validate_cache_directory,
)

PLUGIN_VERSION = "1.2.835.0"
MAX_DOWNLOAD = 32 * 1024 * 1024
EXECUTABLE_MODE = 0o755
LOCK_MODE = 0o600
MEMBER = "sessionmanager-bundle/bin/session-manager-plugin"
# AWS versioned distribution artifacts, verified during release preparation.
TARGETS = {
    "arm64": ("mac_arm64", "7287f20d41eef91643b72dcf28b5f135de49090dd297216a408dc18729e70272"),
    "x86_64": ("mac", "03be79726bbf30ad2a26177d6531636ab6a42f574e0e3326dfb146585cedf260"),
}
BINARY_DIGESTS = {
    "arm64": "cca46503c725072d76cdeb67ec70d4147c5e570a77fbebe6de38c2c8e8463993",
    "x86_64": "77dbb32192f0332c293f56123044720d612a1c8e62bf88ad9e58b90fb8afe867",
}


class PluginAssetError(RuntimeError):
    """The pinned AWS Session Manager plugin is unavailable or unsafe."""


def _private_acl(path: Path, label: str) -> None:
    try:
        _no_acl(path, label)
    except NativeAssetError:
        raise PluginAssetError(f"{label} has unsafe extended ACLs") from None


def _valid(data: bytes, architecture: str) -> None:
    if not struct.calcsize("<8I") <= len(data) <= MAX_DOWNLOAD:
        raise PluginAssetError("AWS plugin executable exceeds size bounds")
    magic, cpu, _, kind = struct.unpack_from("<4I", data)
    if (magic, cpu, kind) != (0xFEEDFACF, MACHO_CPUS[architecture], 2):
        raise PluginAssetError("AWS plugin is not a matching Mach-O executable")
    if hashlib.sha256(data).hexdigest() != BINARY_DIGESTS[architecture]:
        raise PluginAssetError("AWS plugin executable digest does not match its pin")


def plugin_path() -> Path:
    architecture = _architecture()
    return get_bin_path() / f"session-manager-plugin-{PLUGIN_VERSION}-{architecture}"


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
        raise PluginAssetError("Private AWS plugin has unsafe ownership or permissions")
    _private_acl(path, "Private AWS plugin")
    with path.open("rb") as stream:
        _valid(stream.read(MAX_DOWNLOAD + 1), architecture)
    try:
        result = subprocess.run(  # noqa: S603 - digest-verified private executable
            [str(path), "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PluginAssetError("Private AWS plugin version check failed") from None
    if result.returncode or result.stdout.strip() != PLUGIN_VERSION or result.stderr:
        raise PluginAssetError("Private AWS plugin reported an unexpected version")
    return True


def _download_archive(url: str) -> bytes:
    """Download a bounded archive without implicit credentials or unsafe redirects."""
    for _ in range(6):
        try:
            with requests.get(
                url,
                auth=_environment_auth,
                stream=True,
                timeout=(10, 60),
                allow_redirects=False,
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("Location")
                    if not location:
                        raise PluginAssetError("AWS plugin download returned an invalid redirect")
                    following = urljoin(url, location)
                    parsed = urlparse(following)
                    if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                        raise PluginAssetError("AWS plugin download returned an unsafe redirect")
                    url = following
                    continue
                if response.status_code != HTTPStatus.OK:
                    raise PluginAssetError(
                        f"AWS plugin download failed (HTTP {response.status_code})"
                    )
                archive = bytearray()
                for chunk in response.iter_content(65536):
                    archive.extend(chunk)
                    if len(archive) > MAX_DOWNLOAD:
                        raise PluginAssetError("AWS plugin download exceeds its size bound")
                return bytes(archive)
        except requests.RequestException:
            raise PluginAssetError("AWS plugin download failed") from None
    raise PluginAssetError("AWS plugin download exceeded its redirect limit")


def ensure_plugin() -> Path:  # noqa: C901, PLR0912, PLR0915 - locked bounded install transaction
    """Serialize download/verification/publication; killed downloads are never published."""
    import fcntl  # noqa: PLC0415 - macOS-only runtime operation

    architecture = _architecture()
    path = plugin_path()
    try:
        _validate_cache_directory(path.parent)
    except NativeAssetError:
        raise PluginAssetError("Stelvio binary cache is unavailable or unsafe") from None
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(path.parent / "session-manager-plugin.install.lock", flags, 0o600)
    except OSError:
        raise PluginAssetError("Private AWS plugin installation lock is unsafe") from None
    with os.fdopen(fd, "rb+") as lock:
        info = os.fstat(lock.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != LOCK_MODE
        ):
            raise PluginAssetError("Private AWS plugin installation lock is unsafe")
        _private_acl(path.parent / "session-manager-plugin.install.lock", "AWS plugin lock")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if _ready(path, architecture):
                return path
        except PluginAssetError:
            # An owned but corrupt regular file may be replaced by the pinned artifact.
            try:
                info = path.lstat()
            except FileNotFoundError:
                raise PluginAssetError(
                    "Private AWS plugin disappeared during validation"
                ) from None
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise
        target, digest = TARGETS[architecture]
        url = (
            f"https://s3.amazonaws.com/session-manager-downloads/plugin/{PLUGIN_VERSION}/"
            f"{target}/sessionmanager-bundle.zip"
        )
        archive = _download_archive(url)
        if hashlib.sha256(archive).hexdigest() != digest:
            raise PluginAssetError("AWS plugin archive digest does not match its pin")
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                entries = [entry for entry in bundle.infolist() if entry.filename == MEMBER]
                if len(entries) != 1 or entries[0].file_size > MAX_DOWNLOAD:
                    raise PluginAssetError(
                        "AWS plugin archive has an invalid executable inventory"
                    )
                data_buffer = bytearray()
                with bundle.open(entries[0]) as executable:
                    while chunk := executable.read(65536):
                        data_buffer.extend(chunk)
                        if len(data_buffer) > MAX_DOWNLOAD:
                            raise PluginAssetError("AWS plugin executable exceeds size bounds")
                data = bytes(data_buffer)
        except (OSError, RuntimeError, zipfile.BadZipFile):
            raise PluginAssetError("AWS plugin archive is invalid") from None
        _valid(data, architecture)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".ssm-", dir=path.parent, delete=False
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


def installed_plugin() -> Path:
    """Verify without installing; runtime inputs come from the captured user context."""
    path = plugin_path()
    try:
        _validate_cache_directory(path.parent)
    except NativeAssetError:
        raise PluginAssetError("Stelvio binary cache is unavailable or unsafe") from None
    if not _ready(path, _architecture()):
        raise PluginAssetError("Private AWS plugin is missing")
    return path
