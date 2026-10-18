"""Install the downloaded native helper using a fixed system-only bootstrap."""

from __future__ import annotations

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

from stelvio.tunnel.assets import TraforoSelection, downloaded_helper, select_traforo
from stelvio.tunnel.helper_client import HelperBusyError, HelperError, NativeHelper
from stelvio.tunnel.plugin import ensure_plugin

MIN_MACOS_MAJOR = 15
INSTALLED_HELPER = Path("/Library/PrivilegedHelperTools/stelvio-traforo")
SYSTEM_CHANGES = (
    "Install the native helper in /Library/PrivilegedHelperTools/stelvio-traforo; "
    "register dev.stelvio.tunnel with launchd; create its private state and control socket "
    "in /Library/Application Support/Stelvio/tunnel. During a networking session it owns "
    "temporary VPC routes, interfaces, and resolver files."
)


def _supported(*, activation: bool = True) -> None:
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
    try:
        info = INSTALLED_HELPER.lstat()
    except FileNotFoundError:
        return None
    if (
        info.st_uid
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink not in (1, 2)
        or info.st_mode & 0o022
        or info.st_size > 16 * 1024 * 1024
    ):
        raise HelperError("The installed helper path has incompatible ownership or permissions")
    return hashlib.sha256(INSTALLED_HELPER.read_bytes()).hexdigest()


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


def _authorize(script: str) -> None:
    # Lock the existing trusted parent directory: stable across image replacement
    # and uninstall, without leaving a lock artifact or unlinking a live lock.
    prefix = """set -eu
PATH=/usr/bin:/bin
export PATH
parent=/Library/PrivilegedHelperTools
[ ! -L "$parent" ] && [ -d "$parent" ]
[ "$(/usr/bin/stat -f %u "$parent")" = 0 ]
mode=$(/usr/bin/stat -f %Lp "$parent")
[ $((0$mode & 022)) = 0 ]
acl=$(/bin/ls -lde "$parent")
case "$acl" in *+*) exit 1;; esac
"""
    script = prefix + '/usr/bin/lockf -k -n -t 0 "$parent" /bin/sh -c ' + shlex.quote(script)
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
    selection = select_traforo(environment)
    ensure_plugin()
    digest = selection.digest
    installed = _installed_digest()
    if installed == digest and INSTALLED_HELPER.lstat().st_nlink == 1:
        try:
            inspection = NativeHelper().inspect()
        except HelperBusyError:
            # Exact pin plus an authenticated root BUSY reply certifies compatibility.
            return selection
        except (OSError, HelperError):
            pass
        else:
            if inspection.uncertain:
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
            inspection = NativeHelper(timeout=1).inspect()
        except (OSError, HelperError):
            if time.monotonic() >= deadline:
                raise HelperError(
                    "The helper was installed but is not ready; run stlv tunnel inspect"
                ) from None
            time.sleep(0.1)
        else:
            if inspection.uncertain:
                raise HelperError("The installed helper reports unfinished host cleanup")
            return


def uninstall_helper() -> None:
    """Remove the owned installation. Native admission excludes active sessions."""
    _supported(activation=False)
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
