"""Install the downloaded native helper using a fixed system-only bootstrap."""

from __future__ import annotations

import errno
import hashlib
import os
import platform
import shlex
import stat
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

from stelvio.tunnel.assets import (
    NativeAssetError,
    TraforoSelection,
    _no_acl,
    downloaded_helper,
    select_traforo,
)
from stelvio.tunnel.helper_client import HelperBusyError, HelperError, NativeHelper
from stelvio.tunnel.plugin import ensure_plugin

MIN_MACOS_MAJOR = 15
INSTALLED_HELPER = (
    Path("/usr/local/libexec/stelvio-traforo")
    if platform.system() == "Linux"
    else Path("/Library/PrivilegedHelperTools/stelvio-traforo")
)
ADMIN_LOCK = Path("/Library/PrivilegedHelperTools/.stelvio-traforo.admin.lock")
SYSTEM_CHANGES = (
    "Install the native helper in /Library/PrivilegedHelperTools/stelvio-traforo; "
    "register dev.stelvio.tunnel with launchd; create its private state and control socket "
    "in /Library/Application Support/Stelvio/tunnel. During a networking session it owns "
    "temporary VPC routes, interfaces, and resolver files."
)


def system_changes() -> str:
    if platform.system() == "Linux":
        return (
            "Install the native helper in /usr/local/libexec/stelvio-traforo; "
            "register stelvio-traforo.service with systemd; create private state in "
            "/var/lib/stelvio/tunnel and its control socket in /run/stelvio/tunnel. "
            "During a networking session it owns temporary VPC routes, interfaces, "
            "and scoped DNS configuration."
        )
    return SYSTEM_CHANGES


def _supported(*, activation: bool = True) -> None:
    if platform.system() == "Linux":
        from stelvio.tunnel.linux_platform import supported  # noqa: PLC0415

        if not supported(activation=activation) or platform.machine() not in {
            "aarch64",
            "arm64",
            "x86_64",
        }:
            raise HelperError("Linux Traforo requires Debian 13 with systemd and systemd-resolved")
        if not os.geteuid():
            raise HelperError("Run stlv as your ordinary user; only the native helper is elevated")
        return
    major = platform.mac_ver()[0].split(".")[0]
    if (
        platform.system() != "Darwin"
        or platform.machine() not in {"arm64", "x86_64"}
        or (activation and (not major.isdigit() or int(major) < MIN_MACOS_MAJOR))
    ):
        raise HelperError("Traforo networking supports macOS 15+ on arm64 and x86_64")
    if not os.geteuid():
        raise HelperError("Run stlv as your ordinary user; only the native helper is elevated")


def _installed_digest() -> str | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(INSTALLED_HELPER, flags)
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        raise HelperError("The installed helper path is unavailable or unsafe") from None
    try:
        before = os.fstat(descriptor)
        if (
            before.st_uid
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink not in (1, 2)
            or before.st_mode & 0o022
            or not 0 < before.st_size <= 16 * 1024 * 1024
        ):
            raise HelperError(
                "The installed helper path has incompatible ownership or permissions"
            )
        data = bytearray()
        while len(data) <= before.st_size:
            chunk = os.read(descriptor, min(65536, before.st_size + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        if len(data) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise HelperError("The installed helper changed during validation")
        try:
            _no_acl(INSTALLED_HELPER, "installed Traforo")
        except NativeAssetError:
            raise HelperError("The installed helper has unsafe extended ACLs") from None
        current = INSTALLED_HELPER.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino):
            raise HelperError("The installed helper changed during validation")
        return hashlib.sha256(data).hexdigest()
    finally:
        os.close(descriptor)


def _bootstrap(asset: Path, digest: str, *, cleanup: bool = False) -> str:
    # Only fixed system tools and the checked installed image execute as root.
    # Caller paths are quoted data to cp, never privileged executable paths.
    operation = "uninstall" if cleanup else "install"
    source = shlex.quote(str(asset))
    installed = shlex.quote(str(INSTALLED_HELPER))
    return f"""set -eu
PATH=/usr/bin:/bin
export PATH
unset ENV BASH_ENV CDPATH DYLD_LIBRARY_PATH DYLD_INSERT_LIBRARIES
umask 077
for parent in /Library /Library/PrivilegedHelperTools; do
    [ ! -L "$parent" ] && [ -d "$parent" ]
    [ "$(/usr/bin/stat -f %u "$parent")" = 0 ]
    mode=$(/usr/bin/stat -f %Lp "$parent")
    [ $((0$mode & 022)) = 0 ]
    acl=$(/bin/ls -lde "$parent")
    case "$acl" in *+*) exit 1;; esac
done
stage=/Library/PrivilegedHelperTools/.stelvio-traforo.bootstrap.{digest}
retired=/Library/PrivilegedHelperTools/.stelvio-traforo.retired
safe_file() {{
    [ ! -L "$1" ] && [ -f "$1" ]
    [ "$(/usr/bin/stat -f %u "$1")" = 0 ]
    mode=$(/usr/bin/stat -f %Lp "$1")
    [ $((0$mode & 022)) = 0 ]
    acl=$(/bin/ls -le "$1")
    case "$acl" in *+*) exit 1;; esac
}}
# Finish a previously interrupted uninstall before replacing the image.
if [ ! -e {installed} ] && [ ! -L {installed} ] && [ -e "$retired" ]; then
    safe_file "$retired"
    [ "$(/usr/bin/stat -f %l "$retired")" = 1 ]
    /bin/ln "$retired" {installed}
    /bin/rm "$retired"
fi
if [ -e {installed} ] &&
   [ "$(/usr/bin/shasum -a 256 {installed})" != "{digest}  {INSTALLED_HELPER}" ]; then
    safe_file {installed}
    /usr/bin/xattr -p dev.stelvio.traforo.installation {installed} >/dev/null
    current=$(/usr/bin/env -i PATH=/usr/bin:/bin {installed} version)
    /usr/bin/printf '%s\\n' "$current" |
        /usr/bin/grep -Eq '^v[0-9]+\\.[0-9]+\\.[0-9]+(-dev\\+g[0-9a-f]{{7,40}})?$'
    # Validate and retain the replacement before retiring the previous installation.
    if [ ! -e "$stage" ]; then (set -C; : > "$stage"); fi
    safe_file "$stage"
    [ "$(/usr/bin/stat -f %l "$stage")" = 1 ]
    /bin/cp {source} "$stage"
    /bin/chmod 0755 "$stage"
    [ "$(/usr/bin/shasum -a 256 "$stage")" = "{digest}  $stage" ]
    /bin/sync
    /usr/bin/env -i PATH=/usr/bin:/bin {installed} helper uninstall
fi
if [ ! -e {installed} ] && [ ! -L {installed} ]; then
    if [ ! -e "$stage" ] && [ ! -L "$stage" ]; then
        (set -C; : > "$stage")
    fi
    safe_file "$stage"
    [ "$(/usr/bin/stat -f %l "$stage")" = 1 ]
    size=$(/usr/bin/stat -f %z "$stage")
    [ "$size" -le "$(/usr/bin/stat -f %z {source})" ]
    if [ "$size" -gt 0 ]; then
        /usr/bin/head -c "$size" {source} | /usr/bin/cmp - "$stage"
    fi
    /bin/cp {source} "$stage"
    /bin/chmod 0755 "$stage"
    [ "$(/usr/bin/shasum -a 256 "$stage")" = "{digest}  $stage" ]
    /bin/ln "$stage" {installed}
fi
safe_file {installed}
if [ -e "$retired" ] || [ -L "$retired" ]; then
    safe_file "$retired"
    [ "$(/usr/bin/stat -f '%d:%i' "$retired")" = "$(/usr/bin/stat -f '%d:%i' {installed})" ]
    [ "$(/usr/bin/stat -f %l "$retired")" = 2 ]
    [ "$(/usr/bin/shasum -a 256 "$retired")" = "{digest}  $retired" ]
    /bin/rm "$retired"
fi
if [ -e "$stage" ] || [ -L "$stage" ]; then
    safe_file "$stage"
    [ "$(/usr/bin/stat -f '%d:%i' "$stage")" = "$(/usr/bin/stat -f '%d:%i' {installed})" ]
    [ "$(/usr/bin/stat -f %l "$stage")" = 2 ]
    [ "$(/usr/bin/shasum -a 256 "$stage")" = "{digest}  $stage" ]
    /bin/rm "$stage"
fi
[ "$(/usr/bin/stat -f %l {installed})" = 1 ]
[ "$(/usr/bin/shasum -a 256 {installed})" = "{digest}  {INSTALLED_HELPER}" ]
/usr/bin/env -i PATH=/usr/bin:/bin {installed} helper {operation}
"""


def _authorization_script(script: str) -> str:
    # A persistent regular file is stable across image replacement and uninstall;
    # never unlink it, since that could split concurrent administrators across inodes.
    lock = shlex.quote(str(ADMIN_LOCK))
    prefix = f"""set -eu
PATH=/usr/bin:/bin
export PATH
parent=/Library/PrivilegedHelperTools
[ ! -L "$parent" ] && [ -d "$parent" ]
[ "$(/usr/bin/stat -f %u "$parent")" = 0 ]
mode=$(/usr/bin/stat -f %Lp "$parent")
[ $((0$mode & 022)) = 0 ]
acl=$(/bin/ls -lde "$parent")
case "$acl" in *+*) exit 1;; esac
lock={lock}
if [ ! -e "$lock" ] && [ ! -L "$lock" ]; then
    (umask 077; set -C; : > "$lock") 2>/dev/null || :
fi
[ ! -L "$lock" ] && [ -f "$lock" ]
[ "$(/usr/bin/stat -f %u "$lock")" = 0 ]
[ "$(/usr/bin/stat -f %Lp "$lock")" = 600 ]
[ "$(/usr/bin/stat -f %l "$lock")" = 1 ]
acl=$(/bin/ls -le "$lock")
case "$acl" in *+*) exit 1;; esac
"""
    return prefix + '/usr/bin/lockf -k -n -t 0 "$lock" /bin/sh -c ' + shlex.quote(script)


def _authorize(script: str) -> None:
    script = _authorization_script(script)
    # AppleScript string literals accept escaped quotes/backslashes; actual
    # newlines are retained. No shell interpolation of the script argument.
    literal = '"' + script.replace("\\", "\\\\").replace('"', '\\"') + '"'
    result = subprocess.run(  # noqa: S603 - fixed OS authorization API, reviewed bootstrap
        ["/usr/bin/osascript", "-e", f"do shell script {literal} with administrator privileges"],
        capture_output=True,
        text=True,
        timeout=180,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    if result.returncode:
        raise HelperError(
            "Native helper administration did not finish. Close active sessions and retry; "
            "owned host cleanup records are retained. " + result.stderr.strip()
        )


def install_helper(*, environment: Mapping[str, str] | None = None) -> TraforoSelection:
    """Install once; a compatible healthy installation needs no elevation."""
    _supported()
    if platform.system() == "Linux":
        from stelvio.tunnel.linux_installation import (  # noqa: PLC0415 - platform backend
            install_helper as linux_install,
        )

        return linux_install(environment=environment)
    selection = select_traforo(environment)
    ensure_plugin()
    digest = selection.digest
    installed = _installed_digest()
    if installed == digest and INSTALLED_HELPER.lstat().st_nlink == 1:
        try:
            state = NativeHelper().status()
        except HelperBusyError:
            # Exact pin plus an authenticated root BUSY reply certifies compatibility.
            return selection
        except (OSError, HelperError):
            pass
        else:
            if state.uncertain:
                raise HelperError(
                    "The helper has unfinished host cleanup; run stlv tunnel cleanup"
                )
            return selection
    if selection.local_path is not None:
        _authorize(_bootstrap(selection.local_path, digest))
    else:
        with downloaded_helper() as asset:
            _authorize(_bootstrap(asset, digest))
    _wait_ready()
    return selection


def _wait_ready() -> None:
    deadline = time.monotonic() + 15
    while True:
        try:
            state = NativeHelper(timeout=1).status()
        except (OSError, HelperError):
            if time.monotonic() >= deadline:
                raise HelperError(
                    "The helper was installed but is not ready; run stlv tunnel status"
                ) from None
            time.sleep(0.1)
        else:
            if state.uncertain:
                raise HelperError("The installed helper reports unfinished host cleanup")
            return


def uninstall_helper() -> None:
    """Remove the owned installation. Native admission excludes active sessions."""
    _supported(activation=False)
    if platform.system() == "Linux":
        from stelvio.tunnel.linux_installation import (  # noqa: PLC0415 - platform backend
            uninstall_helper as linux_uninstall,
        )

        linux_uninstall()
        return
    installed = _installed_digest()
    # Ordinary removal executes the trusted installed image and works offline.
    # Only interrupted replacement with no installed image needs its release cache.
    retired = Path("/Library/PrivilegedHelperTools/.stelvio-traforo.retired")
    stages = list(INSTALLED_HELPER.parent.glob(".stelvio-traforo.bootstrap.*"))
    if installed is not None:
        _authorize(_bootstrap(INSTALLED_HELPER, installed, cleanup=True))
    elif retired.exists() or stages:
        with downloaded_helper() as asset:
            digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            _authorize(_bootstrap(asset, digest, cleanup=True))
    elif (
        Path("/Library/LaunchDaemons/dev.stelvio.tunnel.plist").exists()
        or Path("/Library/Application Support/Stelvio/tunnel").exists()
    ):
        raise HelperError(
            "The native helper is missing but recovery artifacts remain; "
            "run stlv tunnel install to resume the interrupted installation"
        )
