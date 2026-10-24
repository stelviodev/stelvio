"""Small stdlib-only process identity adapter, usable inside candidate pytest."""

import os
import subprocess


def process_record(pid):
    text = subprocess.check_output(
        ["/bin/ps", "-p", str(pid), "-o", "uid=,pgid=,lstart="], text=True, timeout=5
    ).strip()
    uid, group, started = text.split(maxsplit=2)
    if int(uid) != os.getuid() or int(group) != pid:
        raise RuntimeError("Candidate CLI must be an ordinary-user process group leader")
    return {"pid": pid, "uid": int(uid), "group": int(group), "started": started}
