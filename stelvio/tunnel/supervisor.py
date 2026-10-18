"""Independent VPC workers: readiness, health, reconnection and retained rejection."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING
from uuid import uuid4

from botocore.exceptions import ClientError, HTTPClientError

from stelvio.tunnel.aws_inventory import resolve_vpc_network
from stelvio.tunnel.credentials import AWS_IO
from stelvio.tunnel.discovery import DiscoveryError, DiscoveryPendingError, ResourceDiscovery
from stelvio.tunnel.helper_client import HelperBusyError, HelperError, HelperUnitStatus
from stelvio.tunnel.helper_protocol import MAX_DOMAINS, ResolverEndpoint
from stelvio.tunnel.transport import (
    SshTransport,
    TransportAuthorizationError,
    TransportRetryError,
    classify_aws,
)

if TYPE_CHECKING:
    import boto3

    from stelvio.tunnel.forwarding import Forwarding
    from stelvio.tunnel.helper_client import NativeLease
    from stelvio.tunnel.manifest import SessionDescription, VpcNetwork

HEALTH_SECONDS = 5
DISCOVERY_SECONDS = 30
MAX_BACKOFF = 30
HOST_GENERATION = 1
DNS_EPOCH = 1000000
STARTUP_SECONDS = 600


@dataclass(frozen=True)
class VpcStatus:
    identity: str
    state: str = "starting"
    attempt: int = 0
    cause: str | None = None
    dns_retained: bool = False
    resource_probe: str = "unverified"
    phase: str = "pending"


class VpcWorker:
    def __init__(  # noqa: PLR0913 - independent runtime boundaries
        self,
        description: SessionDescription,
        network: VpcNetwork,
        session: boto3.Session,
        lease: NativeLease,
        forwarding: Forwarding,
        discovery: ResourceDiscovery,
    ) -> None:
        self.description = description
        self.network = network
        self.session = session
        self.lease = lease
        self.forwarding = forwarding
        self.discovery = discovery
        self.stop = Event()
        self._lock = Lock()
        self._status = VpcStatus(network.identity)
        self._thread = Thread(target=self._run, name="stelvio-vpc")
        self.unit = sha256(network.identity.encode()).hexdigest()[:8]
        self.transport = SshTransport(
            network,
            session,
            self.stop,
            ownership={
                "stelvio:app": description.app,
                "stelvio:env": description.environment,
            },
            owner_id=description.session_id,
        )
        self._dns_port: int | None = None
        self._claims: tuple[str, ...] = ()
        self._host_configured = False
        self._host_request: dict | None = None
        self._host_request_confirmed = False
        self._transport_fenced = False
        self._dns_retained = False
        self._stack_attempt = 0
        self._dns_revision = 0
        self._dns_generation = 0
        self._dns_attempt = 0
        self._attempt = 0
        self._ever_ready = False
        self._phase = "pending"

    @property
    def status(self) -> VpcStatus:
        with self._lock:
            return replace(self._status, phase=self._phase)

    def _publish(self, state: str, cause: str | None = None, *, retained: bool = False) -> None:
        with self._lock:
            if state == "ready":
                self._ever_ready = True
            self._status = VpcStatus(
                self.network.identity,
                state,
                self._attempt,
                cause,
                retained,
                "tcp" if state == "ready" and self._resources() else "unverified",
                self._phase,
            )

    def start(self) -> None:
        self._thread.start()

    def _resources(self) -> tuple:
        return tuple(
            resource
            for resource in self.discovery.manifest.resources
            if resource.vpc == self.network.identity
        )

    def _view(self) -> int:
        self._dns_revision += 1
        names = tuple(
            dict.fromkeys(
                (
                    *self._claims,
                    *self.network.dns_domains,
                    *(name for resource in self._resources() for name in resource.hostnames),
                )
            )
        )
        if len(names) > MAX_DOMAINS:
            raise TransportAuthorizationError(
                "VPC DNS ownership exceeds the helper's domain bound"
            )
        self._claims = names
        return self._attempt * DNS_EPOCH + self._dns_revision

    def _configure(self, revision: int) -> None:
        self._dns_port = self.forwarding.configure_dns(
            self.unit, self._attempt, revision, self.network, self._claims
        )
        self._dns_generation = revision
        self._dns_attempt = self._attempt
        # Monotonic claims: old resolver files remain valid during membership
        # refresh/reconnection. Native append-only updates preserve their inodes.
        self._host_request = {
            "unit": self.unit,
            "generation": HOST_GENERATION,
            "vpc_id": self.network.vpc_id,
            "cidrs": tuple(str(cidr) for cidr in self.network.cidrs),
            "resolvers": tuple(ResolverEndpoint(name, self._dns_port) for name in self._claims),
        }
        self._host_request_confirmed = False
        self.lease.configure(**self._host_request)
        self._host_request_confirmed = True
        self._host_configured = True

    def _retire_host(self) -> None:
        if self._host_request is None:
            return
        inspection = self.lease.inspect()
        units = [unit for unit in inspection.units if unit.unit == self.unit]
        if not inspection.owned or (not units and (inspection.uncertain or self._host_configured)):
            raise RuntimeError("Native unit ownership cannot be certified")
        if not units:
            return
        if len(units) != 1 or units[0].generation != HOST_GENERATION:
            raise RuntimeError("Native unit generation changed during retirement")
        # An uncertain reply can follow committed routes/resolver receipts.
        # Replay only this exact journaled request: native PREPARING recovery
        # finishes publication while the relay is already rejecting queries.
        if not self._host_request_confirmed:
            if not self._transport_fenced:
                raise RuntimeError("Cannot resume host publication before transport fencing")
            self.lease.configure(**self._host_request)
            self._host_request_confirmed = True
            self._host_configured = True
        keep_dns = bool(self._host_request["resolvers"])
        self.lease.remove(unit=self.unit, generation=HOST_GENERATION + 1, keep_dns=keep_dns)
        self._dns_retained = keep_dns

    def _dns(self, operation: str, **options: object) -> dict:
        return self.forwarding.dns(
            operation, self.unit, self._dns_attempt, self._dns_generation, **options
        )

    def _probe(self, revision: int) -> None:
        if revision != self._dns_generation:
            raise ValueError("Stale DNS readiness revision")
        self._dns("probe")
        self._dns("probe", direct=True)
        inspection = self.lease.inspect()
        if inspection.uncertain or not any(
            unit.unit == self.unit
            and unit.generation == HOST_GENERATION
            and unit.status == HelperUnitStatus.ACTIVE
            for unit in inspection.units
        ):
            raise TransportRetryError("VPC host configuration is not active")
        for resource in self._resources():
            for hostname in resource.hostnames:
                self._dns("probe", name=hostname, port=resource.ports[0])
                self._dns("probe", name=hostname, port=resource.ports[0], direct=True)
        if self._claims:
            # Unique negative hostname bypasses OS caches. A generation-bound
            # relay observation, not a cached address, proves actual OS routing.
            name = f"stlv-{uuid4().hex}.{self._claims[0]}"
            self._dns("arm", name=name)
            try:
                subprocess.run(  # noqa: S603 - fixed isolated system-resolver probe
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        "import socket,sys; socket.getaddrinfo(sys.argv[1],None)",
                        name,
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if not self._dns("check", name=name).get("observed"):
                    raise TransportRetryError("OS private DNS did not reach this VPC generation")
            finally:
                self._dns("release", name=name)
        if not self.transport.healthy() or not self.forwarding.healthy():
            raise TransportRetryError("VPC transport exited during readiness")

    def _reject(self) -> None:
        self._transport_fenced = False
        failures = []
        if self._dns_generation:
            try:
                self._dns("reject")
            except Exception as error:
                failures.append(error)
        try:
            if self._stack_attempt:
                self.forwarding.remove(self.unit, HOST_GENERATION, self._stack_attempt)
                self._stack_attempt = 0
        except Exception as error:
            failures.append(error)
        try:
            self.transport.close()
        except Exception as error:
            failures.append(error)
        if failures:
            raise RuntimeError("VPC transport cleanup incomplete") from None
        self._transport_fenced = True

    def _attempt_connection(self) -> None:
        self._phase = "vpc-inventory"
        resolved = resolve_vpc_network(self.network, self.session.client("ec2", config=AWS_IO))
        if resolved.cidrs != self.network.cidrs:
            raise TransportAuthorizationError("VPC ranges changed during this session")
        self.discovery.begin(self.network.identity, self._attempt)
        self._phase = "resource-discovery"
        self.discovery.refresh(
            self.network.identity, self._attempt, self.session.client("docdb", config=AWS_IO)
        )
        self._phase = "authenticated-ssh"
        connector = self.transport.start()
        self._phase = "forwarding"
        self.forwarding.add(
            self.unit, HOST_GENERATION, self._attempt, self.network, connector.proxy_port
        )
        self._stack_attempt = self._attempt
        view = self._view()
        self._phase = "host-configuration"
        self._configure(view)
        self._phase = "readiness"
        deadline = time.monotonic() + 30
        while True:
            try:
                self._probe(view)
                break
            except OSError:
                if time.monotonic() >= deadline or self.stop.wait(0.5):
                    raise TransportRetryError(
                        "VPC readiness did not finish within its budget"
                    ) from None
        self._publish("ready")
        refreshed = time.monotonic()
        while not self.stop.wait(HEALTH_SECONDS):
            self._probe(view)
            if time.monotonic() - refreshed >= DISCOVERY_SECONDS:
                self.transport.validate_owner()
                self.discovery.refresh(
                    self.network.identity,
                    self._attempt,
                    self.session.client("docdb", config=AWS_IO),
                )
                view = self._view()
                self._configure(view)
                self._probe(view)
                refreshed = time.monotonic()

    def _run(self) -> None:  # noqa: C901, PLR0912 - explicit retry and terminal cleanup
        backoff = 1
        terminal = None
        startup_deadline = time.monotonic() + STARTUP_SECONDS
        try:
            while not self.stop.is_set():
                self._attempt += 1
                self._publish("starting" if self._attempt == 1 else "retrying")
                try:
                    self._attempt_connection()
                    break
                except ClientError as error:
                    classified = classify_aws(error)
                    if isinstance(classified, TransportAuthorizationError):
                        terminal = "aws-authorization"
                except (TransportAuthorizationError, DiscoveryError, ValueError) as error:
                    if not isinstance(error, DiscoveryPendingError):
                        terminal = "identity-or-configuration"
                except (OSError, HTTPClientError, subprocess.TimeoutExpired, HelperBusyError):
                    pass
                except HelperError as error:
                    status = error.status.name.lower() if error.status else "protocol"
                    terminal = "host-" + status
                    if error.configure_stage is not None:
                        terminal += f"-configure-{error.configure_stage}"
                except Exception as error:
                    terminal = "runtime-boundary-" + type(error).__name__
                if (
                    not terminal
                    and not self.stop.is_set()
                    and not self._ever_ready
                    and time.monotonic() >= startup_deadline
                ):
                    terminal = "startup-timeout"
                self._publish("failed" if terminal else "retrying", terminal)
                self._reject()
                if terminal:
                    break
                if self.stop.wait(backoff):
                    break
                backoff = min(MAX_BACKOFF, backoff * 2)
        except Exception:
            terminal = "cleanup-incomplete"
        finally:
            operations = [self._reject]
            if terminal and not self.stop.is_set():
                operations.append(self._retire_host)
            for operation in operations:
                try:
                    operation()
                except Exception:
                    terminal = "cleanup-incomplete"
            self._publish(
                "failed" if terminal else "stopped",
                terminal,
                retained=self._dns_retained,
            )

    def close(self) -> None:
        self.stop.set()
        self._thread.join(timeout=600)
        if self._thread.is_alive():
            raise TimeoutError("VPC worker has not stopped; keep recovery ownership")

        if self.status.cause == "cleanup-incomplete":
            raise RuntimeError("VPC cleanup incomplete; run stlv tunnel cleanup")


def statuses(workers: tuple[VpcWorker, ...]) -> list[dict]:
    return [asdict(worker.status) for worker in workers]
