"""Opt-in interrupted-VM proof. Run prepare, power-cycle Debian, then verify.

prepare deliberately SIGSTOPs the installed helper after journaling a test unit.
Only run in the dedicated Debian VM. This script never runs automatically.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from stelvio.tunnel.helper_client import HelperError, NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint
from stelvio.tunnel.installation import INSTALLED_HELPER
from tests.integration.linux_host import inventory

mode, location = sys.argv[1:]
directory = Path(location)
helper = NativeHelper(timeout=1)
boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
if mode == "prepare":
    assert sys.platform == "linux" and os.geteuid() != 0
    directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    state = helper.status()
    assert not (state.owned or state.uncertain or state.units)
    before = {
        "boot": boot,
        "binary_sha256": hashlib.sha256(INSTALLED_HELPER.read_bytes()).hexdigest(),
        "host": inventory(),
    }
    (directory / "before.json").write_text(json.dumps(before, indent=2))
    lease = helper.acquire(str(uuid4()))
    lease.configure(
        unit="00000001",
        generation=1,
        vpc_id="vpc-12345678",
        cidrs=("10.254.9.0/24",),
        resolvers=(ResolverEndpoint(uuid4().hex + ".reboot.test", 65130),),
    )
    snapshot = helper.diagnostics()
    assert snapshot["owned"]
    subprocess.run(
        ["/usr/bin/sudo", "/bin/kill", "-STOP", str(snapshot["pid"])], check=True, timeout=5
    )
    (directory / "ready.json").write_text(json.dumps(snapshot, indent=2))
    print("READY: helper stopped with a durable test journal", flush=True)
    while True:
        time.sleep(1)
elif mode == "verify":
    before = json.loads((directory / "before.json").read_text())
    assert boot != before["boot"], "The VM has not rebooted"
    deadline = time.monotonic() + 20
    while True:
        try:
            state = helper.status()
            if not (state.owned or state.uncertain or state.units):
                break
        except (OSError, HelperError):
            pass
        assert time.monotonic() < deadline, "Startup recovery did not finish"
        time.sleep(0.1)
    after = inventory()
    assert {row["ifname"] for row in after["interfaces"]} == {
        row["ifname"] for row in before["host"]["interfaces"]
    }
    assert after["dns"] == before["host"]["dns"]
    assert after["routes"] == before["host"]["routes"]
    assert not Path("/var/lib/stelvio/tunnel/journal.json").exists()
    assert hashlib.sha256(INSTALLED_HELPER.read_bytes()).hexdigest() == before["binary_sha256"]
    result = {
        "outcome": "passed",
        "boot": boot,
        "helper_idle": True,
        "journal_absent": True,
        "host": after,
    }
    (directory / "after.json").write_text(json.dumps(result, indent=2))
    print("Interrupted-VM recovery passed")
else:
    raise ValueError("Use prepare or verify")
