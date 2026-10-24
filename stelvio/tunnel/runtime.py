"""Capture-safe launcher for networking outside the handler process."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from contextlib import suppress
from threading import Lock
from typing import TYPE_CHECKING

from stelvio.tunnel.wire import receive, send, session_to_wire

if TYPE_CHECKING:
    from stelvio.tunnel.credentials import AwsExecutionContext
    from stelvio.tunnel.manifest import SessionDescription

STATUS_TIMEOUT = 2
STOP_TIMEOUT = 45
LEASE_EXIT_TIMEOUT = 20
MAX_RUNTIME_NETWORKS = 8


def _diagnostic_ca() -> str | None:
    from stelvio.exceptions import StelvioProjectError  # noqa: PLC0415
    from stelvio.project import get_dot_stelvio_dir  # noqa: PLC0415
    from stelvio.tunnel.diagnostics.resource_checks import MAX_CA_BYTES  # noqa: PLC0415

    try:
        path = get_dot_stelvio_dir() / "aws/documentdb/global-bundle.pem"
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CA_BYTES:
            return None
        with path.open("rb") as source:
            data = source.read(MAX_CA_BYTES + 1)
        return data.decode("ascii") if len(data) <= MAX_CA_BYTES else None
    except (OSError, UnicodeError, StelvioProjectError):
        return None


class NetworkRuntime:
    def __init__(
        self,
        description: SessionDescription,
        contexts: tuple[AwsExecutionContext, ...],
    ) -> None:
        if not contexts or description.owner_uid != os.geteuid() or not os.geteuid():
            raise ValueError("Network runtime requires captured nonroot AWS contexts")
        environment = contexts[0].process_environment()
        if any(
            context.parent_pid != os.getpid()
            or context.owner_uid != os.geteuid()
            or context.process_environment() != environment
            for context in contexts
        ):
            raise ValueError(
                "Network runtime contexts must share one captured startup environment"
            )
        self._lock = Lock()
        self._closed = False
        self._control, child = socket.socketpair()
        self._lease, lease_child = os.pipe()
        try:
            self.process = subprocess.Popen(  # noqa: S603 - fixed isolated installed runtime entrypoint
                [
                    sys.executable,
                    "-I",
                    "-m",
                    "stelvio.tunnel.runtime_child",
                    str(child.fileno()),
                    str(self._lease),
                ],
                env=environment,
                pass_fds=(child.fileno(), self._lease),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # Terminal Ctrl+C belongs to the CLI. Its finally/EOF ownership
                # path stops this runtime without interrupting AWS cleanup.
                start_new_session=True,
            )
        except BaseException:
            self._control.close()
            child.close()
            os.close(self._lease)
            os.close(lease_child)
            raise
        # Pipe read end is passed; parent keeps only the sole write end. Parent
        # death therefore triggers the child's independent ownership watchdog.
        os.close(self._lease)
        self._lease = lease_child
        child.close()
        self._control.settimeout(10)
        try:
            send(
                self._control,
                {
                    "session": session_to_wire(description),
                    "diagnostic_ca_pem": _diagnostic_ca(),
                    "contexts": [
                        {
                            "provider": context.provider,
                            "account": context.account,
                            "region": context.region,
                            "profile": context.profile,
                            "owner_uid": context.owner_uid,
                            "parent_pid": context.parent_pid,
                        }
                        for context in contexts
                    ],
                },
            )
            response = receive(self._control)
            if response != {"runtime": "started"}:
                raise RuntimeError("Protected network runtime could not start")  # noqa: TRY301
        except BaseException:
            self.close()
            raise

    def status(self) -> list[dict]:
        with self._lock:
            if self._closed:
                raise RuntimeError("Network runtime is closed")
            previous_timeout = self._control.gettimeout()
            self._control.settimeout(STATUS_TIMEOUT)
            try:
                send(self._control, {"operation": "status"})
                response = receive(self._control)
            finally:
                if not self._closed:
                    self._control.settimeout(previous_timeout)
            if response.keys() != {"vpcs"} or not isinstance(response["vpcs"], list):
                raise ValueError("Network runtime returned an invalid status snapshot")
            if len(response["vpcs"]) > MAX_RUNTIME_NETWORKS or any(
                not isinstance(item, dict) for item in response["vpcs"]
            ):
                raise ValueError("Network runtime returned an invalid status snapshot")
            return response["vpcs"]

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            exited = False
            try:
                self._control.settimeout(STATUS_TIMEOUT)
                send(self._control, {"operation": "stop"})
                self.process.wait(timeout=STOP_TIMEOUT)
                exited = True
            except (OSError, subprocess.TimeoutExpired):
                # EOF watchdog closes helper ownership and stops transports
                # before the command exits.
                pass
            finally:
                if self._lease >= 0:
                    with suppress(OSError):
                        os.close(self._lease)
                    self._lease = -1
            try:
                if not exited:
                    self.process.wait(timeout=LEASE_EXIT_TIMEOUT)
                if self.process.returncode != 0:
                    raise RuntimeError("Networking cleanup incomplete; run stlv tunnel cleanup")
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(
                    "Networking shutdown unfinished; run stlv tunnel cleanup"
                ) from error
            finally:
                self._control.close()
