"""Resume owned replacement transactions through the candidate's installer only."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

from .store import digest


def restore_images(installer, status, backup, expected, images):
    """Caller holds the shared host lock. Unknown images/remnants are never removed."""
    current = installer._installed_digest()
    if current is not None and current not in images:
        raise RuntimeError("Installed image is not a recorded test image")
    directory = installer.INSTALLED_HELPER.parent
    remnants = list(directory.glob(".stelvio-traforo.*"))
    remnants = [p for p in remnants if p.name != ".stelvio-traforo.admin.lock"]
    stages = [p for p in remnants if p.name.startswith(".stelvio-traforo.bootstrap.")]
    if len(stages) > 1:
        raise RuntimeError("Multiple staged images: retain evidence for manual recovery")
    for path in remnants:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or info.st_nlink not in (1, 2)
        ):
            raise RuntimeError(f"Unsafe administration remnant: {path}")
        if path.name != ".stelvio-traforo.retired" and path not in stages:
            raise RuntimeError(f"Foreign administration remnant: {path}")
        if path.name == ".stelvio-traforo.retired" and digest(path) not in images:
            raise RuntimeError("Retired image is not recorded")

    def idle():
        state = status()
        if state.owned or state.uncertain or state.mutating or state.units:
            raise RuntimeError("Live or uncertain helper state; replacement refused")

    if current:
        try:
            idle()
        except OSError:
            if not remnants:
                raise  # missing service without a known transaction is uncertain
    if remnants:
        selected = stages[0].name.rsplit(".", 1)[1] if stages else current or next(iter(images))
        if selected not in images:
            raise RuntimeError("Unrecorded staged image; retain ownership evidence")
        path = Path(images[selected])
        if digest(path) != selected:
            raise RuntimeError("Recovery image changed")
        # Installer checks ACLs, journal ownership, partial-file prefixes and locks.
        installer._authorize(installer._bootstrap(path, selected))
        installer._wait_ready()
        idle()
    if backup:
        path = Path(backup)
        if digest(path) != expected:
            raise RuntimeError("Original helper backup changed")
        if installer._installed_digest() != expected:
            installer._authorize(installer._bootstrap(path, expected))
            installer._wait_ready()
        idle()
    elif installer._installed_digest() is not None:
        installer.uninstall_helper()  # installed-image path, never the download fallback


def main():
    import fcntl

    from stelvio.tunnel import installation
    from stelvio.tunnel.helper_client import NativeHelper

    if os.geteuid() == 0:
        raise RuntimeError("Run recovery as the ordinary run owner")
    fd = os.open("/Library/LaunchDaemons", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        restore_images(
            installation,
            lambda: NativeHelper().status(),
            sys.argv[1],
            sys.argv[2],
            json.loads(sys.argv[3]),
        )
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
