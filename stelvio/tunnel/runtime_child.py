"""Protected nonroot networking entrypoint. Never import the application's code."""

from __future__ import annotations

import os
import socket
import sys
import time
from contextlib import ExitStack, suppress
from dataclasses import asdict, replace
from hashlib import sha256
from threading import Event, Lock, Thread
from types import MappingProxyType
from typing import TYPE_CHECKING

from botocore.exceptions import ClientError, HTTPClientError

from stelvio.tunnel.aws_inventory import resolve_vpc_network
from stelvio.tunnel.credentials import AWS_IO, AwsExecutionContext
from stelvio.tunnel.discovery import ResourceDiscovery
from stelvio.tunnel.forwarding import Forwarding, packaged_forwarder
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.manifest import ACCOUNT, NetworkManifest
from stelvio.tunnel.supervisor import VpcStatus, VpcWorker
from stelvio.tunnel.transport import (
    TransportAuthorizationError,
    TransportRetryError,
    classify_aws,
)
from stelvio.tunnel.wire import receive, send, session_from_wire

if TYPE_CHECKING:
    from collections.abc import Callable

    from botocore.client import BaseClient

    from stelvio.tunnel.helper_client import NativeLease
    from stelvio.tunnel.manifest import VpcNetwork

MAX_RUNTIME_NETWORKS = 8
MAX_PROVIDER_NAME = 1024
MAX_REGION_NAME = 64
MAX_PROFILE_NAME = 128


def _owner_status(lease: NativeLease, statuses: list[VpcStatus]) -> list[dict]:
    """Export one authenticated host observation, never a planned generation."""
    host = lease.status()
    if not host.owned or host.uncertain:
        raise RuntimeError("Native host ownership cannot be observed safely")
    units = {unit.unit: unit for unit in host.units}
    values = []
    for status in statuses:
        value = asdict(status)
        unit = units.get(sha256(status.identity.encode()).hexdigest()[:8])
        value["host_unit"] = (
            {"unit": unit.unit, "generation": unit.generation, "status": unit.status.name.lower()}
            if unit is not None
            else None
        )
        values.append(value)
    return values


def _resolve_independent_inventory(
    manifest: NetworkManifest,
    client_for: Callable[[VpcNetwork], BaseClient],
) -> tuple[NetworkManifest, dict[str, str]]:
    """Resolve VPC ranges independently, then fence every known overlap pair."""
    resolved = {}
    failures = {}
    for network in manifest.enabled_vpcs:
        try:
            resolved[network.identity] = resolve_vpc_network(network, client_for(network))
        except Exception:
            failures[network.identity] = "network-inventory"

    updated = replace(
        manifest,
        vpcs=tuple(resolved.get(network.identity, network) for network in manifest.vpcs),
    )
    enabled = updated.enabled_vpcs
    for index, left in enumerate(enabled):
        for right in enabled[index + 1 :]:
            if any(a.overlaps(b) for a in left.cidrs for b in right.cidrs):
                failures[left.identity] = "cidr-overlap"
                failures[right.identity] = "cidr-overlap"
    return updated, failures


def _has_unresolved_inventory(
    pending_inventory: set[str], candidate_inventory: dict[str, VpcNetwork]
) -> bool:
    """Fence every VPC until each retryable inventory has a candidate or retires."""
    return bool(pending_inventory - candidate_inventory.keys())


def run(control: socket.socket, lease_fd: int) -> None:  # noqa: C901, PLR0912, PLR0915 - explicit owner/watchdog lifecycle
    if not os.geteuid() or os.getuid() != os.geteuid():
        raise RuntimeError("Network runtime must run as the ordinary nonroot user")
    control.settimeout(10)
    inputs = receive(control)
    if (
        not isinstance(inputs, dict)
        or inputs.keys() != {"session", "contexts"}
        or not isinstance(inputs["contexts"], list)
    ):
        raise ValueError("Malformed network runtime startup request")
    if len(inputs["contexts"]) > MAX_RUNTIME_NETWORKS:
        raise ValueError("Network runtime provider context limit exceeded")
    description = session_from_wire(inputs["session"])
    if description.owner_uid != os.geteuid():
        raise ValueError("Network runtime owner differs")
    contexts = {}
    context_fields = {"provider", "account", "region", "profile", "owner_uid", "parent_pid"}
    for item in inputs["contexts"]:
        if not isinstance(item, dict) or item.keys() != context_fields:
            raise ValueError("Malformed captured AWS provider context")
        if (
            not isinstance(item["provider"], str)
            or not item["provider"]
            or len(item["provider"]) > MAX_PROVIDER_NAME
            or type(item["account"]) is not str
            or not ACCOUNT.fullmatch(item["account"])
            or type(item["region"]) is not str
            or not item["region"]
            or len(item["region"]) > MAX_REGION_NAME
            or any(char.isspace() or not char.isprintable() for char in item["region"])
            or type(item["owner_uid"]) is not int
            or item["owner_uid"] != os.geteuid()
            or type(item["parent_pid"]) is not int
            or item["parent_pid"] <= 0
            or item["parent_pid"] == os.getpid()
            or (
                item["profile"] is not None
                and (
                    not isinstance(item["profile"], str)
                    or not item["profile"]
                    or len(item["profile"]) > MAX_PROFILE_NAME
                    or any(char.isspace() or not char.isprintable() for char in item["profile"])
                )
            )
        ):
            raise ValueError("Invalid captured AWS provider context identity")
        if item["provider"] in contexts:
            raise ValueError("Duplicate captured AWS provider context")
        contexts[item["provider"]] = AwsExecutionContext(
            **item, _environment=MappingProxyType(dict(os.environ))
        )
    for network in description.manifest.enabled_vpcs:
        captured = contexts[network.provider]
        if captured.account != network.account or captured.region != network.region:
            raise ValueError("VPC provider/account/region differs from captured context")
    stop = Event()
    lock = Lock()
    sdk_lock = Lock()
    fencing_lock = Lock()
    workers = []
    snapshots = {
        network.identity: VpcStatus(network.identity)
        for network in description.manifest.enabled_vpcs
    }
    discovery = ResourceDiscovery(description.manifest)
    with ExitStack() as scope:
        binary = scope.enter_context(packaged_forwarder())
        lease = NativeHelper().acquire(description.session_id)
        scope.callback(lease.close)
        forwarding = Forwarding(binary, lease.carrier)
        scope.callback(forwarding.close)
        inventory_lock = Lock()
        original_inventory: dict[str, VpcNetwork] = {}
        known_inventory: dict[str, VpcNetwork] = {}
        candidate_inventory: dict[str, VpcNetwork] = {}
        pending_inventory: set[str] = set()
        conflicted_inventory: set[str] = set()

        def confirm_inventory(network: VpcNetwork) -> VpcNetwork:
            with inventory_lock:
                if network.identity in conflicted_inventory:
                    raise TransportAuthorizationError("VPC CIDR overlaps another enabled VPC")
                candidate_inventory[network.identity] = network
                overlapping = set()
                for identity, original_peer in original_inventory.items():
                    if identity == network.identity:
                        continue
                    peer = candidate_inventory.get(
                        identity, known_inventory.get(identity, original_peer)
                    )
                    if any(a.overlaps(b) for a in network.cidrs for b in peer.cidrs):
                        overlapping.add(identity)
                if overlapping:
                    conflicted_inventory.update(overlapping | {network.identity})
                    pending_inventory.difference_update(overlapping | {network.identity})
                    with lock:
                        affected = tuple(
                            worker
                            for worker in workers
                            if worker.network.identity in conflicted_inventory
                        )
                    for worker in affected:
                        worker.stop.set()
                        with suppress(Exception):
                            worker.transport.stop_local()
                    raise TransportAuthorizationError("VPC CIDR overlaps another enabled VPC")
                if _has_unresolved_inventory(pending_inventory, candidate_inventory):
                    raise TransportRetryError("Waiting for the other enabled VPC inventories")
                if pending_inventory:
                    known_inventory.update(candidate_inventory)
                    pending_inventory.difference_update(candidate_inventory)
                known_inventory[network.identity] = network
                return network

        def release_inventory(identity: str) -> None:
            with inventory_lock:
                pending_inventory.discard(identity)

        def bootstrap(network: VpcNetwork) -> None:  # noqa: C901 - retry isolated provider bootstrap
            backoff = 1
            while not stop.is_set():
                try:
                    with sdk_lock:
                        session = contexts[network.provider].open_session()
                    if stop.is_set():
                        return
                    worker = VpcWorker(
                        description,
                        network,
                        session,
                        lease,
                        forwarding,
                        discovery,
                        inventory_check=confirm_inventory,
                        inventory_terminal=release_inventory,
                    )
                    with lock:
                        if stop.is_set():
                            return
                        workers.append(worker)
                    worker.start()
                except ClientError as error:
                    if isinstance(classify_aws(error), TransportAuthorizationError):
                        cause = "aws-authorization"
                        retry = False
                    else:
                        cause = "network-inventory"
                        retry = True
                except (OSError, HTTPClientError):
                    cause = "network-inventory"
                    retry = True
                except Exception:
                    cause = "startup-ownership"
                    retry = False
                else:
                    return
                with lock:
                    snapshots[network.identity] = VpcStatus(
                        network.identity,
                        "retrying" if retry else "failed",
                        cause=cause,
                    )
                if not retry:
                    release_inventory(network.identity)
                if not retry or stop.wait(backoff):
                    return
                backoff = min(30, backoff * 2)

        threads: list[Thread] = []

        def prepare() -> None:
            nonlocal description, discovery
            try:
                # Resolve each VPC separately. A failed VPC is excluded while
                # successful inventories are still checked against every known
                # full CIDR range before any worker can add a host route.
                manifest, failures = _resolve_independent_inventory(
                    description.manifest,
                    lambda network: contexts[network.provider]
                    .open_session()
                    .client("ec2", config=AWS_IO),
                )
                description = replace(description, manifest=manifest)
                discovery = ResourceDiscovery(manifest)
                with inventory_lock:
                    original_inventory.update({vpc.identity: vpc for vpc in manifest.enabled_vpcs})
                    known_inventory.update(
                        {
                            identity: vpc
                            for identity, vpc in original_inventory.items()
                            if failures.get(identity) != "network-inventory"
                        }
                    )
                    pending_inventory.update(
                        identity
                        for identity, failure in failures.items()
                        if failure == "network-inventory"
                    )

                with lock:
                    if stop.is_set():
                        return
                    for network in manifest.enabled_vpcs:
                        if failures.get(network.identity) == "cidr-overlap":
                            snapshots[network.identity] = VpcStatus(
                                network.identity,
                                "failed",
                                cause="cidr-overlap",
                            )
                            continue
                        if failures.get(network.identity) == "network-inventory":
                            snapshots[network.identity] = VpcStatus(
                                network.identity,
                                "retrying",
                                cause="network-inventory",
                            )
                        thread = Thread(
                            target=bootstrap, args=(network,), name="stelvio-network-bootstrap"
                        )
                        threads.append(thread)
                        thread.start()
            except Exception:
                with lock:
                    for network in description.manifest.enabled_vpcs:
                        snapshots[network.identity] = VpcStatus(
                            network.identity, "failed", cause="network-inventory"
                        )

        def fence() -> None:
            with fencing_lock:
                while True:
                    with lock:
                        owned_workers = tuple(workers)
                    failures = []
                    for worker in owned_workers:
                        worker.stop.set()
                        try:
                            worker.transport.stop_local()
                        except Exception as error:
                            failures.append(error)
                    if not failures:
                        return
                    # Retain the creator while ownership is uncertain. The
                    # parent reports unfinished shutdown instead of killing it.
                    time.sleep(1)

        def orphaned() -> None:
            os.read(lease_fd, 1)
            stop.set()
            # Kernel authority is revoked immediately, before AWS cleanup waits.
            lease.revoke()
            fence()
            # Wake the runtime's primary loop so it can join workers and run
            # their normal close paths. In particular, SshTransport.close()
            # explicitly terminates the owned SSM session; exiting here would
            # leave that AWS-side session active after its local SSH process died.
            with suppress(OSError):
                control.shutdown(socket.SHUT_RDWR)

        Thread(target=orphaned, daemon=True, name="stelvio-network-owner").start()
        preparer = Thread(target=prepare, name="stelvio-network-inventory")
        preparer.start()
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
                    send(control, {"vpcs": _owner_status(lease, list(current.values()))})
                elif command == {"operation": "stop"}:
                    break
                else:
                    raise ValueError("Invalid network runtime operation")
        except (EOFError, OSError):
            pass
        finally:
            stop.set()
            with lock:
                owned = tuple(workers)
            for worker in owned:
                worker.stop.set()
            failures = []
            preparer.join(timeout=5)
            if preparer.is_alive():
                failures.append(TimeoutError("Network inventory has not stopped"))
            with lock:
                bootstraps = tuple(threads)
            for thread in bootstraps:
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
            try:
                lease.revoke()
            except Exception as error:
                failures.append(error)
            if failures:
                fence()
                raise RuntimeError("Networking cleanup incomplete; retain recovery ownership")
            deadline = time.monotonic() + 30
            helper = NativeHelper(timeout=1)
            while True:
                try:
                    state = helper.status()
                    if not state.owned and not state.uncertain and not state.units:
                        break
                except (OSError, RuntimeError):
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("Host disposal not certified; retain recovery ownership")
                time.sleep(0.1)


def main() -> None:
    try:
        with socket.socket(fileno=int(sys.argv[1])) as control:
            run(control, int(sys.argv[2]))
    except Exception:
        sys.exit(70)


if __name__ == "__main__":
    main()
