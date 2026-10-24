"""Version-pinned AWS Linux plugin packages; no package installer execution."""

from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
import tempfile
from pathlib import Path

TARGETS = {
    "arm64": (
        "ubuntu_arm64",
        "0add94c4c8b6ca63f26e44fd655d662b0f6455a268b5b9ebebee0f462214e928",
        "b365d22b04f477a3016ec608ff7265f8aabc014bcaf41980acd6d3916bddfb50",
    ),
    "x86_64": (
        "ubuntu_64bit",
        "7c6dcad12518571cc7959a713e6a8ae1bdf6ed66fd9bee37dc189e39ca58ae03",
        "f6002be08e5c57dc97eb9a0ef819d54f4c9a4a724c5818cec7d4baeaadfe4cbd",
    ),
}
AR_HEADER_BYTES = 60
MAX_AR_MEMBERS = 16
MAX_TAR_MEMBERS = 4096
PRIVATE_MODE = 0o600
MEMBER = "usr/local/sessionmanagerplugin/bin/session-manager-plugin"


def executable(archive: bytes) -> bytes:  # noqa: C901 - bounded package parser
    from stelvio.tunnel.plugin import MAX_DOWNLOAD, PluginAssetError  # noqa: PLC0415

    if not archive.startswith(b"!<arch>\n") or len(archive) > MAX_DOWNLOAD:
        raise PluginAssetError("Invalid bounded Debian package")
    offset, members = 8, {}
    while offset < len(archive):
        header = archive[offset : offset + 60]
        if (
            len(header) != AR_HEADER_BYTES
            or header[58:] != b"`\n"
            or len(members) >= MAX_AR_MEMBERS
        ):
            raise PluginAssetError("Invalid Debian package header")
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58])
        offset += 60
        if size < 0 or offset + size > len(archive) or name in members:
            raise PluginAssetError("Invalid Debian package member")
        members[name] = archive[offset : offset + size]
        offset += size + size % 2
    payloads = [value for name, value in members.items() if name.startswith("data.tar.")]
    if len(payloads) != 1 or members.get("debian-binary") != b"2.0\n":
        raise PluginAssetError("Invalid Debian package inventory")
    found = None
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payloads[0]), mode="r|*") as bundle:
            for index, item in enumerate(bundle):
                total += item.size
                if index >= MAX_TAR_MEMBERS or total > 4 * MAX_DOWNLOAD:
                    raise PluginAssetError("Debian package expands beyond bounds")
                if item.name.removeprefix("./") != MEMBER:
                    continue
                if found is not None or not item.isfile() or not 0 < item.size <= MAX_DOWNLOAD:
                    raise PluginAssetError("Invalid Linux plugin executable member")
                stream = bundle.extractfile(item)
                if stream is None:
                    raise PluginAssetError("Missing Linux plugin executable")
                found = stream.read(MAX_DOWNLOAD + 1)
    except (tarfile.TarError, OSError, ValueError) as error:
        raise PluginAssetError("Invalid Linux plugin package") from error
    if found is None:
        raise PluginAssetError("Missing Linux plugin executable")
    return found


def validate(data: bytes, architecture: str) -> None:
    from stelvio.tunnel.linux_platform import validate_elf  # noqa: PLC0415
    from stelvio.tunnel.plugin import MAX_DOWNLOAD, PluginAssetError  # noqa: PLC0415

    try:
        validate_elf(data, architecture, limit=MAX_DOWNLOAD)
    except ValueError as error:
        raise PluginAssetError(str(error)) from error
    if hashlib.sha256(data).hexdigest() != TARGETS[architecture][2]:
        raise PluginAssetError("Linux AWS plugin executable digest does not match its pin")


def ensure() -> Path:
    import fcntl  # noqa: PLC0415

    from stelvio.tunnel import plugin  # noqa: PLC0415

    architecture = plugin._architecture()  # noqa: SLF001
    path = plugin.plugin_path()
    plugin._validate_cache_directory(path.parent)  # noqa: SLF001
    fd = os.open(
        path.parent / "session-manager-plugin-linux.install.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    with os.fdopen(fd, "rb+") as lock:
        info = os.fstat(lock.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) != PRIVATE_MODE
        ):
            raise plugin.PluginAssetError("Unsafe Linux plugin installation lock")
        plugin._private_acl(  # noqa: SLF001 - shared trusted-path boundary
            path.parent / "session-manager-plugin-linux.install.lock", "Linux plugin lock"
        )
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if plugin._ready(path, architecture):  # noqa: SLF001
                return path
        except plugin.PluginAssetError:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
                raise
        target, checksum, _ = TARGETS[architecture]
        url = f"https://s3.amazonaws.com/session-manager-downloads/plugin/{plugin.PLUGIN_VERSION}/{target}/session-manager-plugin.deb"
        archive = plugin._download_archive(url)  # noqa: SLF001
        if hashlib.sha256(archive).hexdigest() != checksum:
            raise plugin.PluginAssetError("Linux AWS plugin package digest does not match its pin")
        data = executable(archive)
        validate(data, architecture)
        pending = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=".ssm-linux-", dir=path.parent, delete=False
            ) as stream:
                pending = Path(stream.name)
                stream.write(data)
                os.fchmod(stream.fileno(), 0o755)
                stream.flush()
                os.fsync(stream.fileno())
            plugin._ready(pending, architecture)  # noqa: SLF001
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
