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
 [ ! -L "$1" ] || exit 1
 [ -d "$1" ] || exit 1
 [ "$(stat -c %u "$1")" = 0 ] || exit 1
 mode=$(stat -c %a "$1"); [ $((0$mode & 022)) = 0 ]
 acl=$(getfacl -cp "$1")
 acl=$(printf '%s\\n' "$acl" | sed '/^user::/d; /^group::/d; /^other::/d; /^$/d')
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
 [ ! -L "$1" ] || exit 1
 [ -f "$1" ] || exit 1
 [ "$(stat -c %u "$1")" = 0 ] || exit 1
 [ "$(stat -c %h "$1")" = 1 ] || exit 1
 mode=$(stat -c %a "$1"); [ $((0$mode & 022)) = 0 ]
 acl=$(getfacl -cp "$1")
 acl=$(printf '%s\\n' "$acl" | sed '/^user::/d; /^group::/d; /^other::/d; /^$/d')
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
service=/etc/systemd/system/stelvio-traforo.service
receipt=/var/lib/stelvio/tunnel/installation.json
# Unit drop-ins can change ExecStart/ExecStop without changing the certified
# service file. Refuse both on-disk and currently loaded overrides.
bases="/etc/systemd/system /run/systemd/system"
bases="$bases /usr/local/lib/systemd/system /usr/lib/systemd/system"
for base in $bases; do
 for unit in service.d stelvio-.service.d stelvio-traforo.service.d; do
  [ ! -e "$base/$unit" ] || exit 1
  [ ! -L "$base/$unit" ] || exit 1
 done
done
wants=/etc/systemd/system/multi-user.target.wants
enabled_link="$wants/stelvio-traforo.service"
if [ -e "$wants" ] || [ -L "$wants" ]; then safe_dir "$wants"; fi
# Certify all existing installation resources before stopping any service.
# Root ownership alone does not make an unrelated installation ours.
verify_receipt() {{
 safe_file "$receipt"
 [ "$(wc -c < "$receipt")" -le 1024 ]
 jq -e '.version == 1 and (.digest | test("^[0-9a-f]{{64}}$")) and
  (.service_sha256 | test("^[0-9a-f]{{64}}$"))' "$receipt" >/dev/null
 [ "$(sha256sum "$installed" | cut -d ' ' -f 1)" = "$(jq -r .digest "$receipt")" ]
 [ "$(stat -c %d "$installed")" = "$(jq -r .device "$receipt")" ]
 [ "$(stat -c %i "$installed")" = "$(jq -r .inode "$receipt")" ]
}}
had_image=no
was_active=no
was_enabled=disabled
if [ -e "$installed" ] || [ -L "$installed" ]; then
 safe_file "$installed"
 safe_file "$service"
 verify_receipt
 [ "$(sha256sum "$service" | cut -d ' ' -f 1)" = "$(jq -r .service_sha256 "$receipt")" ]
 [ "$(systemctl show -p FragmentPath --value stelvio-traforo.service)" = "$service" ]
 [ -z "$(systemctl show -p DropInPaths --value stelvio-traforo.service)" ]
 if [ -e "$enabled_link" ] || [ -L "$enabled_link" ]; then
  [ -L "$enabled_link" ] || exit 1
  [ "$(stat -c %u "$enabled_link")" = 0 ] || exit 1
  [ "$(readlink -f "$enabled_link")" = "$service" ]
 fi
 if systemctl is-active --quiet stelvio-traforo.service; then was_active=yes; fi
 was_enabled=$(systemctl is-enabled stelvio-traforo.service) || :
 case "$was_enabled" in enabled|disabled) ;; *) exit 1;; esac
 had_image=yes
else
 [ ! -e "$service" ] || exit 1
 [ ! -L "$service" ] || exit 1
 [ ! -e "$receipt" ] || exit 1
 [ ! -L "$receipt" ] || exit 1
 [ ! -e "$enabled_link" ] || exit 1
 [ ! -L "$enabled_link" ] || exit 1
 [ "$(systemctl show -p LoadState --value stelvio-traforo.service)" = not-found ]
fi
if [ {shlex.quote(operation)} = uninstall ]; then
 safe_file "$installed"
 [ "$(sha256sum "$installed" | cut -d ' ' -f 1)" = {shlex.quote(digest)} ]
 "$installed" helper uninstall
 exit
fi
retired=/usr/local/libexec/.stelvio-traforo.retired
[ ! -e "$retired" ] || exit 1
[ ! -L "$retired" ] || exit 1
stage=$(mktemp /usr/local/libexec/.stelvio-traforo.XXXXXXXX)
backup=$(mktemp -d /var/lib/stelvio/tunnel/.transaction.XXXXXXXX)
had_service=no
had_receipt=no
replaced=no
if [ -e /etc/systemd/system/stelvio-traforo.service ]; then
 safe_file /etc/systemd/system/stelvio-traforo.service
 cp -p /etc/systemd/system/stelvio-traforo.service "$backup/service"
 had_service=yes
fi
if [ -e /var/lib/stelvio/tunnel/installation.json ]; then
 safe_file /var/lib/stelvio/tunnel/installation.json
 cp -p /var/lib/stelvio/tunnel/installation.json "$backup/receipt"
 had_receipt=yes
fi
remove_backup() {{
 for f in "$backup/service" "$backup/receipt"; do
  if [ -e "$f" ]; then safe_file "$f"; rm "$f"; fi
 done
 rmdir "$backup"
}}
rollback() {{
 rc=$?
 rm -f "$stage"
 if [ "$rc" != 0 ] && [ -e "$retired" ]; then
  safe_file "$retired"
  systemctl stop stelvio-traforo.service || true
  mv -f "$retired" "$installed"
  if [ "$had_service" = yes ]; then
   safe_file "$backup/service"
   cp -p "$backup/service" /etc/systemd/system/stelvio-traforo.service
  fi
  if [ "$had_receipt" = yes ]; then
   safe_file "$backup/receipt"
   cp -p "$backup/receipt" /var/lib/stelvio/tunnel/installation.json
  fi
  systemctl daemon-reload
  if [ "$was_enabled" = enabled ]; then
   systemctl enable stelvio-traforo.service
  else
   systemctl disable stelvio-traforo.service
  fi
  if [ "$was_active" = yes ]; then systemctl start stelvio-traforo.service; fi
 elif [ "$rc" != 0 ] && [ "$had_image" = no ] && [ "$replaced" = yes ]; then
  safe_file "$installed"
  [ "$(sha256sum "$installed" | cut -d ' ' -f 1)" = {shlex.quote(digest)} ]
  # A partial first install has no retired image. Remove only resources
  # certified by its new receipt; retain uncertain evidence on any mismatch.
  if [ -e "$receipt" ] || [ -L "$receipt" ]; then
   verify_receipt
   if [ -e "$service" ] || [ -L "$service" ]; then
    safe_file "$service"
    [ "$(sha256sum "$service" | cut -d ' ' -f 1)" = "$(jq -r .service_sha256 "$receipt")" ]
    systemctl disable --now stelvio-traforo.service
    [ ! -e /var/lib/stelvio/tunnel/journal.json ]
    rm "$service"
   fi
   rm "$receipt"
  else
   [ ! -e "$service" ] || exit 1
   [ ! -L "$service" ] || exit 1
  fi
  rm "$installed"
  systemctl daemon-reload
 fi
 remove_backup
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
replaced=yes
"$installed" helper install
if [ -e "$retired" ]; then safe_file "$retired"; rm "$retired"; fi
remove_backup
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
