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
from pathlib import Path

import requests

from stelvio.pulumi import get_bin_path
from stelvio.tunnel.assets import MACHO_CPUS, _architecture
from stelvio.tunnel.helper_client import HelperError

PLUGIN_VERSION = "1.2.707.0"
MAX_DOWNLOAD = 32 * 1024 * 1024
EXECUTABLE_MODE = 0o755
MEMBER = "sessionmanager-bundle/bin/session-manager-plugin"
# AWS versioned distribution artifacts, verified during release preparation.
TARGETS = {
    "arm64": ("mac_arm64", "ed8fddd305df8122ed462adc38458fc4e1d75c224c4a65a57fafc4638255b71f"),
    "x86_64": ("mac", "c285834745f100702e9369c5b17766382a91595133ec8f4198e1e5a4b5fce977"),
}
BINARY_DIGESTS = {
    "arm64": "5457434601469f24f7e6660453a956f4b88e40d780c0291f2fa10c43d54e492a",
    "x86_64": "80f001add28033f6a6f6c397e367702e4eff6739fe5b0620ecac65a0ebd3b224",
}


def _valid(data: bytes, architecture: str) -> None:
    if not struct.calcsize("<8I") <= len(data) <= MAX_DOWNLOAD:
        raise HelperError("AWS plugin executable exceeds size bounds")
    magic, cpu, _, kind = struct.unpack_from("<4I", data)
    if (magic, cpu, kind) != (0xFEEDFACF, MACHO_CPUS[architecture], 2):
        raise HelperError("AWS plugin is not a matching Mach-O executable")
    if hashlib.sha256(data).hexdigest() != BINARY_DIGESTS[architecture]:
        raise HelperError("AWS plugin executable digest does not match its pin")


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
        or info.st_mode & 0o777 != EXECUTABLE_MODE
    ):
        raise HelperError("Private AWS plugin has unsafe ownership or permissions")
    with path.open("rb") as stream:
        _valid(stream.read(MAX_DOWNLOAD + 1), architecture)
    result = subprocess.run(  # noqa: S603 - digest-verified private executable
        [str(path), "--version"], capture_output=True, text=True, timeout=10, check=False
    )
    if result.returncode or result.stdout.strip() != PLUGIN_VERSION:
        raise HelperError("Private AWS plugin reported an unexpected version")
    return True


def ensure_plugin() -> Path:  # noqa: C901, PLR0915 - locked bounded install transaction
    """Serialize download/verification/publication; killed downloads are never published."""
    import fcntl  # noqa: PLC0415 - macOS-only runtime operation

    architecture = _architecture()
    path = plugin_path()
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path.parent / "session-manager-plugin.install.lock", flags, 0o600)
    with os.fdopen(fd, "rb+") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
            raise HelperError("Private AWS plugin installation lock is unsafe")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if _ready(path, architecture):
                return path
        except HelperError:
            # An owned but corrupt regular file may be replaced by the pinned artifact.
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
                raise
        target, digest = TARGETS[architecture]
        url = (
            f"https://s3.amazonaws.com/session-manager-downloads/plugin/{PLUGIN_VERSION}/"
            f"{target}/sessionmanager-bundle.zip"
        )
        archive = bytearray()
        try:
            with requests.get(url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                for chunk in response.iter_content(65536):
                    archive.extend(chunk)
                    if len(archive) > MAX_DOWNLOAD:
                        raise HelperError("AWS plugin download exceeds its size bound")
        except requests.RequestException as error:
            raise HelperError("AWS plugin download failed; retry stlv tunnel install") from error
        if hashlib.sha256(archive).hexdigest() != digest:
            raise HelperError("AWS plugin archive digest does not match its pin")
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            entries = [entry for entry in bundle.infolist() if entry.filename == MEMBER]
            if len(entries) != 1 or entries[0].file_size > MAX_DOWNLOAD:
                raise HelperError("AWS plugin archive has an invalid executable inventory")
            data = bundle.read(entries[0])
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
    if not _ready(path, _architecture()):
        raise HelperError("Private AWS plugin is missing; run stlv tunnel install")
    return path
