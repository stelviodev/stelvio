"""Own the packaged nonroot carrier process and acknowledge stack changes."""

from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import time
from contextlib import contextmanager
from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import socket
    from collections.abc import Iterator
    from pathlib import Path

    from stelvio.tunnel.manifest import VpcNetwork

MAX_REPLY = 32


@contextmanager
def packaged_forwarder() -> Iterator[Path]:
    """Both roles use the exact verified root-owned installed Traforo image."""
    from stelvio.tunnel.assets import packaged_helper  # noqa: PLC0415 - lazy platform assets
    from stelvio.tunnel.installation import (  # noqa: PLC0415 - lazy platform installation
        INSTALLED_HELPER,
        _installed_digest,
    )

    with packaged_helper() as asset:
        expected = hashlib.sha256(asset.read_bytes()).hexdigest()
        if _installed_digest() != expected:
            raise RuntimeError("The installed Traforo differs; clean up and install this package")
        yield INSTALLED_HELPER


class Forwarding:
    def __init__(self, binary: Path, carrier: socket.socket) -> None:
        if not os.geteuid():
            raise RuntimeError("Forwarding must run without root privileges")
        self._lock = Lock()
        self.process = subprocess.Popen(  # noqa: S603 - verified packaged nonroot artifact
            [str(binary), "forwarder", str(carrier.fileno())],
            pass_fds=(carrier.fileno(),),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        try:
            if self._reply() != b"READY\n":
                raise RuntimeError("Nonroot forwarder failed to start")  # noqa: TRY301
        except BaseException:
            self.close()
            raise

    def _reply(self) -> bytes:
        result = bytearray()
        deadline = time.monotonic() + 5
        while len(result) < MAX_REPLY:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.process.stdout], [], [], remaining)[0]:
                raise TimeoutError("Nonroot forwarder acknowledgement timed out")
            byte = os.read(self.process.stdout.fileno(), 1)
            if not byte:
                raise ConnectionError("Nonroot forwarder exited")
            result.extend(byte)
            if byte == b"\n":
                return bytes(result)
        raise ConnectionError("Nonroot forwarder returned invalid acknowledgement")

    def _command(self, data: dict) -> None:
        with self._lock:
            self.process.stdin.write(json.dumps(data).encode() + b"\n")
            self.process.stdin.flush()
            if self._reply() != b"OK\n":
                raise RuntimeError("Nonroot forwarder rejected configuration")

    def add(
        self, unit: str, host_generation: int, attempt: int, network: VpcNetwork, port: int
    ) -> None:
        self._command(
            {
                "operation": "add",
                "unit": int(unit, 16),
                "generation": host_generation,
                "attempt": attempt,
                "proxy": f"127.0.0.1:{port}",
                "cidrs": [str(cidr) for cidr in network.cidrs],
            }
        )

    def remove(self, unit: str, host_generation: int, attempt: int) -> None:
        self._command(
            {
                "operation": "remove",
                "unit": int(unit, 16),
                "generation": host_generation,
                "attempt": attempt,
            }
        )

    def healthy(self) -> bool:
        return self.process.poll() is None

    def close(self) -> None:
        if self.process.stdin:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        if self.process.stdout:
            self.process.stdout.close()
