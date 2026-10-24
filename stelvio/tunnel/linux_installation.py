"""Linux administration through a checked system-only sudo bootstrap."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from stelvio.tunnel.assets import TraforoSelection

from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.linux_platform import INSTALLED_HELPER


def bootstrap(source: Path, digest: str, *, remove: bool = False) -> str:
    operation = "uninstall" if remove else "install"
    return f"""set -eu
PATH=/usr/bin:/bin
export PATH
unset ENV BASH_ENV CDPATH LD_PRELOAD LD_LIBRARY_PATH PYTHONPATH
umask 077
safe_dir() {{
 [ ! -L "$1" ] && [ -d "$1" ] && [ "$(stat -c %u "$1")" = 0 ]
 mode=$(stat -c %a "$1"); [ $((0$mode & 022)) = 0 ]
 acl=$(getfacl -cp "$1" | sed '/^user::/d; /^group::/d; /^other::/d; /^$/d')
 [ -z "$acl" ]
}}
for d in /usr /usr/local /var /var/lib /run /etc /etc/systemd /etc/systemd/system; do
 safe_dir "$d"
done
directories="/usr/local/libexec /var/lib/stelvio /var/lib/stelvio/tunnel"
directories="$directories /run/stelvio /run/stelvio/tunnel"
for d in $directories; do
 [ -e "$d" ] || mkdir -m 755 "$d"
 safe_dir "$d"
done
safe_file() {{
 [ ! -L "$1" ] && [ -f "$1" ]
 [ "$(stat -c %u "$1")" = 0 ] && [ "$(stat -c %h "$1")" = 1 ]
 mode=$(stat -c %a "$1"); [ $((0$mode & 022)) = 0 ]
 acl=$(getfacl -cp "$1" | sed '/^user::/d; /^group::/d; /^other::/d; /^$/d')
 [ -z "$acl" ]
}}
lock=/var/lib/stelvio/tunnel/bootstrap.lock
if [ -e "$lock" ] || [ -L "$lock" ]; then safe_file "$lock"; fi
exec 9>"$lock"
flock -n 9
# Hold the same admission gate as acquisition across replacement and rollback.
gate=/var/lib/stelvio/tunnel/admission.lock
if [ -e "$gate" ] || [ -L "$gate" ]; then safe_file "$gate"; fi
exec 8>"$gate"
flock -n -x 8
[ ! -e /var/lib/stelvio/tunnel/journal.json ]
installed=/usr/local/libexec/stelvio-traforo
if [ {shlex.quote(operation)} = uninstall ]; then
 safe_file "$installed"
 [ "$(sha256sum "$installed" | cut -d ' ' -f 1)" = {shlex.quote(digest)} ]
 "$installed" helper uninstall
 exit
fi
retired=/usr/local/libexec/.stelvio-traforo.retired
[ ! -e "$retired" ] && [ ! -L "$retired" ]
stage=$(mktemp /usr/local/libexec/.stelvio-traforo.XXXXXXXX)
rollback() {{
 rc=$?
 rm -f "$stage"
 if [ "$rc" != 0 ] && [ -e "$retired" ]; then
  safe_file "$retired"
  mv -f "$retired" "$installed"
  "$installed" helper install || true
 fi
 exit "$rc"
}}
trap rollback EXIT
cp -- {shlex.quote(str(source))} "$stage"
chown root:root "$stage"
chmod 755 "$stage"
safe_file "$stage"
[ "$(sha256sum "$stage" | cut -d ' ' -f 1)" = {shlex.quote(digest)} ]
if [ -e "$installed" ] || [ -L "$installed" ]; then
 safe_file "$installed"
 systemctl stop stelvio-traforo.service
 [ ! -e /var/lib/stelvio/tunnel/journal.json ]
 mv "$installed" "$retired"
fi
mv "$stage" "$installed"
"$installed" helper install
if [ -e "$retired" ]; then safe_file "$retired"; rm "$retired"; fi
trap - EXIT
"""


def authorize(script: str) -> None:
    result = subprocess.run(
        ["/usr/bin/sudo", "/bin/sh", "-s"],
        input=script,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
        env={"PATH": "/usr/bin:/bin"},
    )
    if result.returncode:
        raise HelperError(
            "Linux helper administration failed; recovery evidence is retained. "
            + result.stderr[-4096:]
        )


def install_helper(*, environment: Mapping[str, str] | None = None) -> TraforoSelection:
    from stelvio.tunnel import installation  # noqa: PLC0415
    from stelvio.tunnel.assets import select_traforo  # noqa: PLC0415
    from stelvio.tunnel.plugin import ensure_plugin  # noqa: PLC0415

    selection = select_traforo(environment)
    ensure_plugin()
    current = installation._installed_digest()  # noqa: SLF001
    if current == selection.digest:
        try:
            state = NativeHelper(timeout=1).status()
        except (HelperError, OSError):
            pass
        else:
            if state.uncertain:
                raise HelperError("The helper has unfinished cleanup; run stlv tunnel cleanup")
            return selection
    if selection.local_path is None:
        raise HelperError("Linux installation requires STLV_TRAFORO_DEV_BINARY")
    authorize(bootstrap(selection.local_path, selection.digest))
    installation._wait_ready()  # noqa: SLF001
    return selection


def uninstall_helper() -> None:
    from stelvio.tunnel import installation  # noqa: PLC0415

    digest = installation._installed_digest()  # noqa: SLF001
    if digest is None:
        if Path("/var/lib/stelvio/tunnel/installation.json").exists():
            raise HelperError("The helper image is missing; retain its installation receipt")
        return
    authorize(bootstrap(INSTALLED_HELPER, digest, remove=True))
