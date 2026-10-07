# ruff: noqa: S101, T201, PT017
"""Kill only the owned native launchd helper; recover its journal and resolver receipts."""

import json
import os
import socket
import time
from contextlib import ExitStack, suppress
from pathlib import Path
from uuid import uuid4

from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint
from stelvio.tunnel.installation import _authorize


def main() -> None:
    assert os.geteuid() != 0
    session = str(uuid4())
    token = session.replace("-", "")
    domain = f"p3restart-{token}.invalid"
    baseline = set(socket.if_nameindex())
    helper = NativeHelper(timeout=3)
    files = Path("/private/etc/resolver")
    with ExitStack() as cleanup:
        old = helper.acquire(session)

        def close_old() -> None:
            with suppress(HelperError, OSError):
                old.close()

        cleanup.callback(close_old)
        old.configure(
            unit="d0000004",
            generation=1,
            vpc_id="vpc-00000004",
            cidrs=("10.254.0.0/16",),
            resolvers=(ResolverEndpoint(domain, 65132),),
        )
        previous = set(files.glob(f"stelvio.{token}.*"))
        assert len(previous) == 1
        added = set(socket.if_nameindex()) - baseline
        assert len(added) == 1
        _authorize(
            "set -eu\numask 077\n"
            "(set -C; /usr/bin/printf STLVJNL > "
            "'/Library/Application Support/Stelvio/tunnel/journal.next')\n"
            "/bin/launchctl kill SIGKILL system/dev.stelvio.tunnel\n"
        )
        deadline = time.monotonic() + 25
        while True:
            try:
                new = helper.acquire(session)
            except (HelperError, OSError):
                assert time.monotonic() < deadline
                time.sleep(0.1)
            else:
                break
        cleanup.callback(new.close)
        assert not Path("/Library/Application Support/Stelvio/tunnel/journal.next").exists()
        assert new.capability != old.capability
        value = helper.inspect()
        assert not value.units
        assert not value.uncertain
        assert not (added & set(socket.if_nameindex()))
        assert not any(path.exists() for path in previous)
        try:
            old.close()
        except HelperError as error:
            assert str(error) == "Native helper request failed: unauthorized"
        else:
            raise AssertionError("Old capability accepted after daemon restart")
        new.configure(
            unit="d0000004",
            generation=2,
            vpc_id="vpc-00000004",
            cidrs=("10.254.0.0/16",),
            resolvers=(ResolverEndpoint(domain, 65132),),
        )
        assert [
            (unit.unit, unit.generation, unit.status.name) for unit in helper.inspect().units
        ] == [("d0000004", 2, "ACTIVE")]
    assert not helper.inspect().owned
    assert not list(files.glob(f"stelvio.{token}.*"))
    print(
        json.dumps(
            {
                "session": session,
                "uid": os.geteuid(),
                "passed": (
                    "native daemon crash and partial journal: owned resolver recovery, "
                    "capability rotation, stale rejection, fresh configuration and cleanup"
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
