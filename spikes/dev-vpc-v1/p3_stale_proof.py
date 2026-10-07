# ruff: noqa: S101, S603, T201
"""A killed nonroot owner cannot retain a kernel interface via a carrier copy."""

import array
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import ExitStack

from stelvio.tunnel.helper_client import HelperError, NativeHelper

CHILD = """import array,os,socket,sys,time
from uuid import uuid4
from stelvio.tunnel.helper_client import NativeHelper
control=socket.socket(fileno=int(sys.argv[1]))
lease=NativeHelper(timeout=10).acquire(str(uuid4()))
lease.configure(unit="c0000003",generation=10,vpc_id="vpc-00000003",cidrs=("10.254.0.0/16",))
control.sendmsg([b"ready"],[(socket.SOL_SOCKET,socket.SCM_RIGHTS,array.array("i",[lease.carrier.fileno()]))])
while True: time.sleep(1)
"""


def main() -> None:
    assert os.geteuid() != 0
    baseline = set(socket.if_nameindex())
    with ExitStack() as cleanup:
        parent, child_socket = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        cleanup.callback(parent.close)
        cleanup.callback(child_socket.close)
        parent.settimeout(15)
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", CHILD, str(child_socket.fileno())],
            pass_fds=(child_socket.fileno(),),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

        def stop_child() -> None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            if child.stderr:
                child.stderr.close()

        cleanup.callback(stop_child)
        child_socket.close()
        data, controls, flags, _ = parent.recvmsg(128, socket.CMSG_SPACE(16))
        assert data == b"ready"
        assert not flags
        descriptors = array.array("i")
        assert len(controls) == 1
        level, kind, payload = controls[0]
        assert level == socket.SOL_SOCKET
        assert kind == socket.SCM_RIGHTS
        descriptors.frombytes(payload)
        assert len(descriptors) == 1
        carrier = socket.socket(fileno=descriptors[0])
        cleanup.callback(carrier.close)
        added = set(socket.if_nameindex()) - baseline
        assert len(added) == 1, added
        child.kill()
        child.wait(timeout=5)
        deadline = time.monotonic() + 12
        while True:
            try:
                value = NativeHelper(timeout=1).inspect()
            except (HelperError, OSError):
                value = None
            if value is not None and not value.owned and not value.uncertain and not value.units:
                break
            assert time.monotonic() < deadline, value
            time.sleep(0.05)
        assert not (added & set(socket.if_nameindex()))
        assert carrier.fileno() >= 0  # Held nonroot copy cannot keep the root utun alive.
        print(
            json.dumps(
                {
                    "uid": os.geteuid(),
                    "removed_interfaces": sorted(added),
                    "passed": "killed owner recovery with retained nonroot carrier",
                }
            )
        )


if __name__ == "__main__":
    main()
