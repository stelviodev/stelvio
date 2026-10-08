"""Capture-safe launcher for networking outside the handler process."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from threading import Lock
from typing import TYPE_CHECKING

from stelvio.tunnel.wire import receive, send, session_to_wire

if TYPE_CHECKING:
    from stelvio.tunnel.credentials import AwsExecutionContext
    from stelvio.tunnel.manifest import SessionDescription


class NetworkRuntime:
    def __init__(
        self,
        description: SessionDescription,
        contexts: tuple[AwsExecutionContext, ...],
        *,
        access_settings: dict | None = None,
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
                    "access_settings": access_settings,
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
            send(self._control, {"operation": "status"})
            return receive(self._control)["vpcs"]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            try:
                send(self._control, {"operation": "stop"})
                # Host authority is released by the child immediately. AWS
                # instance/IAM deletion needs its bounded reconciliation waits.
                self.process.wait(timeout=600)
            except (OSError, subprocess.TimeoutExpired):
                # EOF watchdog closes helper ownership and stops registered
                # actors while its creator is still alive, before process exit.
                os.close(self._lease)
                self._lease = -1
                try:
                    self.process.wait(timeout=30)
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError(
                        "Networking shutdown unfinished; retain recovery ownership"
                    ) from error
            finally:
                self._control.close()
                if self._lease >= 0:
                    os.close(self._lease)
                    self._lease = -1
            if self.process.returncode != 0:
                raise RuntimeError("Networking cleanup incomplete; retain AWS recovery ownership")
