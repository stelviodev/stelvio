"""Independent VPC workers: readiness, health, reconnection and retained rejection."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from hashlib import sha256
from ipaddress import IPv4Address
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING
from uuid import uuid4

import dns.message
import dns.rcode
import dns.rdatatype
from botocore.exceptions import ClientError

from stelvio.tunnel.aws_inventory import resolve_vpc_network
from stelvio.tunnel.credentials import AWS_IO
from stelvio.tunnel.discovery import DiscoveryError, DiscoveryPendingError, ResourceDiscovery
from stelvio.tunnel.dns import DnsListener, DnsRelay, DnsView
from stelvio.tunnel.helper_client import HelperBusyError, HelperUnitStatus
from stelvio.tunnel.helper_protocol import MAX_DOMAINS, ResolverEndpoint
from stelvio.tunnel.transport import (
    SshTransport,
    TransportAuthorizationError,
    TransportRetryError,
    classify_aws,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    import boto3

    from stelvio.tunnel.forwarding import Forwarding
    from stelvio.tunnel.helper_client import NativeLease
    from stelvio.tunnel.manifest import SessionDescription, VpcNetwork
    from stelvio.tunnel.socks import SocksConnector

HEALTH_SECONDS = 5
DISCOVERY_SECONDS = 30
MAX_BACKOFF = 30
HOST_GENERATION = 1
DNS_EPOCH = 1000000


@dataclass(frozen=True)
class VpcStatus:
    identity: str
    state: str = "starting"
    attempt: int = 0
    cause: str | None = None
    dns_retained: bool = False
    resource_probe: str = "unverified"


class VpcWorker:
    def __init__(  # noqa: PLR0913 - independent runtime boundaries
        self,
        description: SessionDescription,
        network: VpcNetwork,
        session: boto3.Session,
        lease: NativeLease,
        forwarding: Forwarding,
        discovery: ResourceDiscovery,
        *,
        cleanup: Callable[[], None],
        record: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.description = description
        self.network = network
        self.session = session
        self.lease = lease
        self.forwarding = forwarding
        self.discovery = discovery
        self.cleanup = cleanup
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
            record=record,
            owner_id=description.session_id,
        )
        self.relay: DnsRelay | None = None
        self.listener: DnsListener | None = None
        self._claims: tuple[str, ...] = ()
        self._host_configured = False
        self._stack_attempt = 0
        self._dns_revision = 0
        self._dns_generation = 0
        self._attempt = 0

    @property
    def status(self) -> VpcStatus:
        with self._lock:
            return self._status

    def _publish(self, state: str, cause: str | None = None, *, retained: bool = False) -> None:
        with self._lock:
            self._status = VpcStatus(
                self.network.identity,
                state,
                self._attempt,
                cause,
                retained,
                "tcp" if state == "ready" and self._resources() else "unverified",
            )

    def start(self) -> None:
        self._thread.start()

    def _resources(self) -> tuple:
        return tuple(
            resource
            for resource in self.discovery.manifest.resources
            if resource.vpc == self.network.identity
        )

    def _view(self, connector: SocksConnector) -> DnsView:
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
        return DnsView(
            self._attempt * DNS_EPOCH + self._dns_revision,
            self.network.dns_domains,
            names,
            IPv4Address(int(self.network.cidrs[0].network_address) + 2),
            connector,
        )

    def _configure(self, view: DnsView) -> None:
        if self.relay is None:
            self.relay = DnsRelay(view)
            self.listener = DnsListener(self.relay)
            self.listener.start()
        else:
            self.relay.activate(view)
        self._dns_generation = view.generation
        # Monotonic claims: old resolver files remain valid during membership
        # refresh/reconnection. Native append-only updates preserve their inodes.
        self.lease.configure(
            unit=self.unit,
            generation=HOST_GENERATION,
            vpc_id=self.network.vpc_id,
            cidrs=tuple(str(cidr) for cidr in self.network.cidrs),
            resolvers=tuple(ResolverEndpoint(name, self.listener.port) for name in self._claims),
        )
        self._host_configured = True

    def _probe(self, view: DnsView) -> None:  # noqa: C901 - independent readiness evidence
        inspection = self.lease.helper.inspect()
        if inspection.uncertain or not any(
            unit.unit == self.unit
            and unit.generation == HOST_GENERATION
            and unit.status == HelperUnitStatus.ACTIVE
            for unit in inspection.units
        ):
            raise TransportRetryError("VPC host configuration is not active")
        for resource in self._resources():
            for hostname in resource.hostnames:
                query = dns.message.make_query(hostname, "A")
                answer = dns.message.from_wire(self.relay.answer(query.to_wire(), udp=False))
                addresses = [
                    IPv4Address(record.address)
                    for records in answer.answer
                    if records.rdtype == dns.rdatatype.A
                    for record in records
                ]
                if answer.rcode() != dns.rcode.NOERROR or not addresses:
                    raise TransportRetryError("VPC resource private DNS is unavailable")
                for address in addresses:
                    if not any(address in cidr for cidr in self.network.cidrs):
                        raise TransportAuthorizationError(
                            "Private DNS returned a resource outside its VPC"
                        )
                with view.connector.connect(addresses[0], resource.ports[0], timeout=3):
                    pass
                reached = subprocess.run(  # noqa: S603 - ordinary host TCP must traverse helper/carrier/stack
                    [
                        sys.executable,
                        "-I",
                        "-S",
                        "-c",
                        "import socket,sys; "
                        "socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=3).close()",
                        str(addresses[0]),
                        str(resource.ports[0]),
                    ],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
                if reached.returncode:
                    raise TransportRetryError(
                        "Ordinary host TCP did not traverse the VPC forwarding path"
                    )
        if self._claims:
            # Unique negative hostname bypasses OS caches. A generation-bound
            # relay observation, not a cached address, proves actual OS routing.
            name = f"stlv-{uuid4().hex}.{self._claims[0]}"
            observed = self.relay.expect_query(name, view.generation)
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
                if not observed.is_set():
                    raise TransportRetryError("OS private DNS did not reach this VPC generation")
            finally:
                self.relay.forget_query(name)
        if not self.transport.healthy() or not self.forwarding.healthy():
            raise TransportRetryError("VPC transport exited during readiness")

    def _reject(self) -> None:
        if self.relay is not None:
            self.relay.reject(self._dns_generation)
        failures = []
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

    def _attempt_connection(self) -> None:
        resolved = resolve_vpc_network(self.network, self.session.client("ec2", config=AWS_IO))
        if resolved.cidrs != self.network.cidrs:
            raise TransportAuthorizationError("VPC ranges changed during this session")
        self.discovery.begin(self.network.identity, self._attempt)
        self.discovery.refresh(
            self.network.identity, self._attempt, self.session.client("docdb", config=AWS_IO)
        )
        connector = self.transport.start()
        self.forwarding.add(
            self.unit, HOST_GENERATION, self._attempt, self.network, connector.proxy_port
        )
        self._stack_attempt = self._attempt
        view = self._view(connector)
        self._configure(view)
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
                view = self._view(connector)
                self._configure(view)
                self._probe(view)
                refreshed = time.monotonic()

    def _run(self) -> None:  # noqa: C901, PLR0912 - explicit retry and terminal cleanup
        backoff = 1
        terminal = None
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
                except (OSError, subprocess.TimeoutExpired, HelperBusyError):
                    pass
                except Exception:
                    terminal = "runtime-boundary"
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
            if terminal and self._host_configured:
                operations.append(
                    lambda: self.lease.remove(
                        unit=self.unit, generation=HOST_GENERATION + 1, keep_dns=True
                    )
                )
            operations.append(self.cleanup)
            for operation in operations:
                try:
                    operation()
                except Exception:
                    terminal = "cleanup-incomplete"
            self._publish(
                "failed" if terminal else "stopped",
                terminal,
                retained=bool(terminal and self._host_configured),
            )

    def close(self) -> None:
        self.stop.set()
        self._thread.join(timeout=10)
        if self._thread.is_alive():
            raise TimeoutError("VPC worker has not stopped; keep recovery ownership")
        if self.listener:
            self.listener.close()
        if self.status.cause == "cleanup-incomplete":
            raise RuntimeError("VPC cleanup incomplete; retain AWS recovery ownership")


def statuses(workers: tuple[VpcWorker, ...]) -> list[dict]:
    return [asdict(worker.status) for worker in workers]
