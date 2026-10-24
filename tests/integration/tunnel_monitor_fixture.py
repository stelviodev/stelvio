"""Real monitor process on a private pseudo-terminal; owns no tunnel resources."""

import contextlib
import fcntl
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import time
from threading import Thread

from stelvio.tunnel.diagnostics.runtime import discover, snapshot
from stelvio.tunnel.monitor.model import Dashboard


class MonitorProcess:
    def __init__(self, session_id, *, log_path):
        master, slave = pty.openpty()
        self.master = master
        self.output = bytearray()
        self.log_path = log_path
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 160, 0, 0))
        try:
            self.process = subprocess.Popen(  # noqa: S603 - installed monitor entry point, literal UUID
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "from stelvio.cli import main; main()",
                    "tunnel",
                    "monitor",
                    "--session",
                    session_id,
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=dict(os.environ, TERM="xterm-256color"),
                start_new_session=True,
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self.reader = Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self):
        with self.log_path.open("wb") as output:
            while True:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                if not data:
                    return
                output.write(data)
                output.flush()
                self.output.extend(data)
                if len(self.output) > 512 * 1024:
                    del self.output[: -256 * 1024]

    def wait(self, text, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if text.encode() in self.output:
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        raise AssertionError(f"Monitor did not show {text!r}: {self.output[-5000:]!r}")

    def close(self, *, kill=False):
        try:
            if self.process.poll() is None:
                if kill:
                    self.process.send_signal(signal.SIGKILL)
                else:
                    os.write(self.master, b"q")
                self.process.wait(timeout=10)
            assert self.process.returncode == (-signal.SIGKILL if kill else 0)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
            with contextlib.suppress(OSError):
                os.close(self.master)
            self.reader.join(timeout=1)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def app_snapshot(app):
    for path in discover():
        try:
            value = snapshot(path)
        except (OSError, ValueError, EOFError):
            continue
        if value["session"].get("app") == app:
            return value
    raise AssertionError(f"No diagnostics session for {app}")


def assert_monitor_vpcs(value, *, ready):
    model = Dashboard()
    model.update(value, time.monotonic())
    assert len(model.rows) == 2
    assert sum(row["state"] == "ready" for row in model.rows) == ready
    assert sum(row["dns_healthy"] for row in model.rows) == ready
    return model
