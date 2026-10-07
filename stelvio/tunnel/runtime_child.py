"""Protected nonroot networking entrypoint. Never import the application's code."""

from __future__ import annotations

import os
import socket
import sys
from contextlib import ExitStack
from dataclasses import asdict, replace
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import TYPE_CHECKING

from stelvio.tunnel.access_runtime import RuntimeAccess
from stelvio.tunnel.credentials import AwsExecutionContext
from stelvio.tunnel.discovery import ResourceDiscovery
from stelvio.tunnel.forwarding import Forwarding, packaged_forwarder
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.supervisor import VpcStatus, VpcWorker
from stelvio.tunnel.wire import receive, send, session_from_wire

if TYPE_CHECKING:
    from stelvio.tunnel.manifest import VpcNetwork


def run(control: socket.socket, lease_fd: int) -> None:  # noqa: C901, PLR0912, PLR0915 - explicit owner/watchdog lifecycle
    if not os.geteuid() or os.getuid() != os.geteuid():
        raise RuntimeError("Network runtime must run as the ordinary nonroot user")
    control.settimeout(10)
    inputs = receive(control)
    description = session_from_wire(inputs["session"])
    if description.owner_uid != os.geteuid():
        raise ValueError("Network runtime owner differs")
    contexts = {
        item["provider"]: AwsExecutionContext(
            **item, _environment=MappingProxyType(dict(os.environ))
        )
        for item in inputs["contexts"]
    }
    for network in description.manifest.enabled_vpcs:
        captured = contexts[network.provider]
        if captured.account != network.account or captured.region != network.region:
            raise ValueError("VPC provider/account/region differs from captured context")
    settings = inputs["access_settings"]
    stop = Event()
    lock = Lock()
    sdk_lock = Lock()
    workers = []
    accesses = []
    snapshots = {
        network.identity: VpcStatus(network.identity)
        for network in description.manifest.enabled_vpcs
    }
    discovery = ResourceDiscovery(description.manifest)
    with ExitStack() as scope:
        lease = NativeHelper().acquire(description.session_id)
        scope.callback(lease.close)
        binary = scope.enter_context(packaged_forwarder())
        forwarding = Forwarding(binary, lease.carrier)
        scope.callback(forwarding.close)

        def bootstrap(network: VpcNetwork) -> None:
            access = None
            worker = None
            try:
                context = contexts[network.provider]
                # SDK provider initialization serializes; the original refresh
                # chain remains alive in this process, outside handlers.
                with sdk_lock:
                    session = context.open_session()
                if network.policy == BastionPolicy.TEMPORARY:
                    if settings is None:
                        raise ValueError(  # noqa: TRY301 - classified at per-VPC boundary
                            "Temporary VPC access requires independent state ownership"
                        )
                    access = RuntimeAccess(
                        description,
                        network,
                        context,
                        contexts[settings["home_provider"]],
                        settings,
                    )
                    with lock:
                        accesses.append(access)
                    network = replace(network, access=access.start())
                if stop.is_set():
                    if access:
                        access.close()
                    return
                worker = VpcWorker(
                    description,
                    network,
                    session,
                    lease,
                    forwarding,
                    discovery,
                    cleanup=access.close if access else lambda: None,
                    record=access.journal.record if access else None,
                )
                with lock:
                    if stop.is_set():
                        worker.stop.set()
                    workers.append(worker)
                worker.start()
            except Exception:
                if access and worker is None:
                    try:
                        access.close()
                    except Exception:
                        with lock:
                            snapshots[network.identity] = VpcStatus(
                                network.identity, "failed", cause="cleanup-incomplete"
                            )
                        return
                with lock:
                    snapshots[network.identity] = VpcStatus(
                        network.identity, "failed", cause="startup-ownership"
                    )

        threads = [
            Thread(target=bootstrap, args=(network,), name="stelvio-network-bootstrap")
            for network in description.manifest.enabled_vpcs
        ]

        def orphaned() -> None:
            os.read(lease_fd, 1)
            stop.set()
            # Kernel authority is revoked immediately, before AWS cleanup waits.
            lease.closed = True
            lease.connection.close()
            lease.carrier.close()
            with lock:
                owned_workers, owned_accesses = tuple(workers), tuple(accesses)
            failures = []
            for worker in owned_workers:
                worker.stop.set()
                try:
                    worker.transport.stop_local()
                except Exception as error:
                    failures.append(error)
            for access in owned_accesses:
                try:
                    access.abort()
                except Exception as error:
                    failures.append(error)
            if not failures:
                os._exit(70)
            # Creator remains alive when any actor could not be fenced. Recovery
            # records and the parent-visible incomplete exit are retained.

        Thread(target=orphaned, daemon=True, name="stelvio-network-owner").start()
        for thread in threads:
            thread.start()
        send(control, {"runtime": "started"})
        control.settimeout(None)
        try:
            while True:
                command = receive(control)
                if command == {"operation": "status"}:
                    with lock:
                        current = dict(snapshots)
                        current.update(
                            {worker.network.identity: worker.status for worker in workers}
                        )
                    send(control, {"vpcs": [asdict(status) for status in current.values()]})
                elif command == {"operation": "stop"}:
                    break
                else:
                    raise ValueError("Invalid network runtime operation")
        except EOFError:
            pass
        finally:
            stop.set()
            with lock:
                owned = tuple(workers)
            for worker in owned:
                worker.stop.set()
            failures = []
            for thread in threads:
                thread.join(timeout=5)
                if thread.is_alive():
                    failures.append(TimeoutError("Network bootstrap has not stopped"))
            with lock:
                owned = tuple(workers)
            for worker in owned:
                try:
                    worker.close()
                except Exception as error:
                    failures.append(error)
            if failures:
                raise RuntimeError("Networking cleanup incomplete; retain recovery ownership")


def main() -> None:
    try:
        with socket.socket(fileno=int(sys.argv[1])) as control:
            run(control, int(sys.argv[2]))
    except Exception:
        sys.exit(70)


if __name__ == "__main__":
    main()
