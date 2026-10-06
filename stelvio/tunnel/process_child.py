"""Nonprivileged exec barrier: no imports of application or AWS code."""

import os
import sys

# Spawned with Python -I -S: no PYTHONPATH, site or user startup hooks. Only the
# parent's successful durable registration permits execution of the native actor.
if os.read(0, 1) != b"1":
    sys.exit(1)
null = os.open(os.devnull, os.O_RDONLY)
os.dup2(null, 0)
os.close(null)
os.execve(sys.argv[1], sys.argv[1:], os.environ)  # noqa: S606 - nonroot actor verified by spawning registry
