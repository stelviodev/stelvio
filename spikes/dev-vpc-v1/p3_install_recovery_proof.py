# ruff: noqa: S101, T201
"""Interrupt the reviewed bootstrap after publication, then recover with normal commands."""

import hashlib
import json
import os
import shlex
from pathlib import Path
from uuid import uuid4

from stelvio.tunnel.assets import packaged_helper
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.installation import (
    INSTALLED_HELPER,
    SYSTEM_CHANGES,
    _authorize,
    _bootstrap,
    cleanup_helper,
    install_helper,
)

PUBLISHED_LINKS = 2


def main() -> None:
    assert os.geteuid() != 0
    assert not INSTALLED_HELPER.exists()
    cleanup_helper()  # Repeated cleanup is a no-op at the agreed absent baseline.
    print(SYSTEM_CHANGES, flush=True)
    with packaged_helper() as asset:
        digest = hashlib.sha256(asset.read_bytes()).hexdigest()
        stage = Path(f"/Library/PrivilegedHelperTools/.dev.stelvio.tunnel.bootstrap.{digest}")
        assert not stage.exists()
        bootstrap = _bootstrap(asset, digest)
        # Stop exactly after exclusive hard-link publication, before removing
        # the staging link. Only the reviewed system-only bootstrap is elevated.
        marker = "\nsafe_file " + shlex.quote(str(INSTALLED_HELPER)) + "\n"
        partial = bootstrap.split(marker, 1)[0] + "\n"
        assert partial.endswith("fi\n")
        try:
            _authorize(partial)
            image, staging = INSTALLED_HELPER.lstat(), stage.lstat()
            assert image.st_uid == 0
            assert image.st_nlink == PUBLISHED_LINKS
            assert (image.st_dev, image.st_ino) == (staging.st_dev, staging.st_ino)
            install_helper()
            assert not stage.exists()
            assert INSTALLED_HELPER.lstat().st_nlink == 1
            helper = NativeHelper(timeout=5)
            assert not helper.inspect().uncertain
            lease = helper.acquire(str(uuid4()))
            lease.close()
        finally:
            cleanup_helper()
    cleanup_helper()
    assert not INSTALLED_HELPER.exists()
    print(
        json.dumps(
            {
                "uid": os.geteuid(),
                "helper_sha256": digest,
                "passed": (
                    "interrupted hard-link publication recovery, reinstall/use, "
                    "complete uninstall and repeated cleanup"
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
