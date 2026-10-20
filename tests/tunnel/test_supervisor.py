"""Generation-fenced VPC reconnects and readiness with isolated adapters."""

from ipaddress import IPv4Network
from types import SimpleNamespace

from pytest import raises

from stelvio.tunnel.evidence import record_transport_event
from stelvio.tunnel.helper_client import HelperError, HelperStatus, HelperUnitStatus
from stelvio.tunnel.manifest import NetworkManifest, ResourceNetwork
from stelvio.tunnel.supervisor import VpcWorker
from stelvio.tunnel.transport import (
    TransportArtifactError,
    TransportCleanupError,
    TransportRetryError,
)


def _worker_world(  # noqa: C901 - models a cross-adapter VPC lifecycle in one fixture
    monkeypatch, *, fail_first=False, configure_uncertain=False, close_uncertain=False
):
    events = []
    dns_state = {"rejected": False, "observed": False}
    network = SimpleNamespace(
        identity="network-a",
        cidrs=(IPv4Network("10.254.0.0/16"),),
        dns_domains=("private.example.com",),
        vpc_id="vpc-owned",
    )
    resource = ResourceNetwork(
        "db",
        "network-a",
        "documentdb",
        "cluster",
        (27017,),
        ("db.private.example.com",),
        ("sg-database",),
    )
    discovery = SimpleNamespace(
        manifest=NetworkManifest((), (resource,), ()),
        begin=lambda *args: None,
        refresh=lambda *args: None,
    )

    class Lease:
        unit = None
        uncertain_once = configure_uncertain

        def status(self):
            return SimpleNamespace(
                owned=True,
                uncertain=False,
                mutating=False,
                units=() if self.unit is None else (SimpleNamespace(**self.unit),),
            )

        def configure(self, **request):
            events.append(("host-configure", request.copy()))
            if self.unit is None:
                self.unit = {
                    "unit": request["unit"],
                    "generation": request["generation"],
                    "status": HelperUnitStatus.ACTIVE,
                    "request": request.copy(),
                }
            elif self.unit["status"] == HelperUnitStatus.ACTIVE:
                if request["generation"] != self.unit["generation"]:
                    raise HelperError("active unit generation changed")
                old_resolvers = self.unit["request"]["resolvers"]
                if request["resolvers"][: len(old_resolvers)] != old_resolvers:
                    raise HelperError("DNS claims were dropped")
                self.unit["request"] = request.copy()
            else:
                if request["generation"] <= self.unit["generation"]:
                    raise HelperError("stale generation")
                self.unit = {
                    "unit": request["unit"],
                    "generation": request["generation"],
                    "status": HelperUnitStatus.ACTIVE,
                    "request": request.copy(),
                }
            if self.uncertain_once:
                self.uncertain_once = False
                raise HelperError(
                    "reply lost after durable configure", status=HelperStatus.UNCERTAIN
                )

        def remove(self, *, unit, generation, keep_dns):
            events.append(("host-remove", unit, generation, keep_dns))
            if self.unit is None or self.unit["status"] != HelperUnitStatus.ACTIVE:
                raise HelperError("unit is not active")
            if generation <= self.unit["generation"]:
                raise HelperError("stale removal")
            self.unit = {
                "unit": unit,
                "generation": generation,
                "status": HelperUnitStatus.RETAINED if keep_dns else HelperUnitStatus.INACTIVE,
                "request": self.unit["request"],
            }

    lease = Lease()

    class Transport:
        attempt = 0
        checks = 0

        def start(self):
            self.attempt += 1
            self.checks = 0
            events.append(("transport-start", self.attempt))
            if dns_state["rejected"] and self.attempt > 1:
                events.append(("reconnect-after-reject", self.attempt))
            return SimpleNamespace(proxy_port=32123, cidrs=network.cidrs)

        def healthy(self):
            self.checks += 1
            return not (fail_first and self.attempt == 1 and self.checks >= 2)

        def close(self):
            events.append(("transport-close", self.attempt))
            if close_uncertain:
                raise TransportCleanupError("owned transport cleanup uncertain")

    transport = Transport()
    transport_options = []

    def make_transport(*args, **kwargs):
        transport_options.append(kwargs)
        return transport

    monkeypatch.setattr("stelvio.tunnel.supervisor.SshTransport", make_transport)
    monkeypatch.setattr(
        "stelvio.tunnel.supervisor.resolve_vpc_network", lambda *args, **kwargs: network
    )
    monkeypatch.setattr("stelvio.tunnel.supervisor.HEALTH_SECONDS", 0.001)
    monkeypatch.setattr("stelvio.tunnel.supervisor.DISCOVERY_SECONDS", 1000)

    class Forwarding:
        host_generation = 0
        attempt = 0
        revision = 0
        port = 32124
        claims = ()

        def add(self, unit, generation, attempt, configured_network, port):
            if generation <= self.host_generation or attempt <= self.attempt:
                raise ValueError("stale forwarder attempt")
            self.host_generation, self.attempt = generation, attempt
            events.append(("forward-add", generation, attempt))

        def configure_dns(  # noqa: PLR0913 - mirrors the forwarder protocol signature
            self, unit, generation, attempt, revision, configured_network, names
        ):
            if (generation, attempt) != (self.host_generation, self.attempt):
                raise ValueError("stale DNS configuration")
            if revision <= self.revision:
                raise ValueError("stale DNS revision")
            self.revision, self.claims = revision, tuple(names)
            events.append(("dns-configure", generation, attempt, revision, tuple(names)))
            dns_state["rejected"] = False
            return self.port

        def dns(self, operation, unit, generation, attempt, revision, **options):
            if (generation, attempt, revision) != (
                self.host_generation,
                self.attempt,
                self.revision,
            ):
                raise ValueError("stale DNS frame")
            events.append(("dns-" + operation, generation, attempt, revision, options.copy()))
            if operation == "reject":
                dns_state["rejected"] = True
            if operation == "check":
                return {"observed": dns_state["observed"]}
            return {}

        def remove(self, unit, generation, attempt):
            events.append(("forward-remove", generation, attempt))

        def healthy(self):
            return True

    forwarding = Forwarding()
    worker = VpcWorker(
        SimpleNamespace(app="app", environment="dev", session_id="session"),
        network,
        SimpleNamespace(client=lambda *args, **kwargs: None),
        lease,
        forwarding,
        discovery,
    )
    monkeypatch.setattr(
        "stelvio.tunnel.supervisor.subprocess.run",
        lambda *args, **kwargs: dns_state.update(observed=True) or SimpleNamespace(returncode=0),
    )
    return SimpleNamespace(
        worker=worker,
        lease=lease,
        transport=transport,
        forwarding=forwarding,
        events=events,
        dns_state=dns_state,
        transport_options=transport_options,
    )


def test_reconnect_advances_generation_and_keeps_dns_rejected_until_ready(monkeypatch):
    world = _worker_world(monkeypatch, fail_first=True)
    worker = world.worker
    published = []
    publish = worker._publish

    def observe(state, cause=None, **options):
        publish(state, cause, **options)
        published.append(worker.status)
        if state == "ready" and worker.status.attempt == 2:
            worker.stop.set()

    monkeypatch.setattr(worker, "_publish", observe)
    worker.start()
    worker._thread.join(5)
    assert not worker._thread.is_alive()
    worker.close()

    assert world.transport_options[0]["record"] is record_transport_event
    configurations = [e for e in world.events if e[0] == "host-configure"]
    assert [entry[1]["generation"] for entry in configurations] == [1, 3]
    assert all(entry[1]["resolvers"][0].port == world.forwarding.port for entry in configurations)
    assert [e[1:3] for e in world.events if e[0] == "forward-add"] == [(1, 1), (3, 2)]
    assert world.events.index(("dns-reject", 1, 1, 1, {})) < world.events.index(
        ("transport-start", 2)
    )
    assert world.events.index(("transport-close", 1)) < world.events.index(
        ("host-remove", worker.unit, 2, True)
    )
    assert world.dns_state["rejected"]
    with raises(ValueError, match="stale DNS frame"):
        world.forwarding.dns("probe", worker.unit, 1, 1, 1)
    assert worker.status.state == "stopped"
    assert worker.status.dns_retained
    assert [(s.state, s.attempt) for s in published if s.state == "ready"] == [
        ("ready", 1),
        ("ready", 2),
    ]


def test_inventory_gate_retries_before_forwarding_and_adopts_verified_cidrs(monkeypatch):
    world = _worker_world(monkeypatch)
    worker = world.worker
    inventory_checks = []
    aws_inventory_checks = []
    real_wait = worker.stop.wait

    def fast_wait(timeout=None):
        return real_wait(min(timeout or 0.001, 0.001))

    monkeypatch.setattr(worker.stop, "wait", fast_wait)

    def resolve_inventory(network, _client):
        aws_inventory_checks.append(network.identity)
        if len(aws_inventory_checks) == 1:
            raise OSError("temporary VPC inventory outage")
        return network

    monkeypatch.setattr("stelvio.tunnel.supervisor.resolve_vpc_network", resolve_inventory)

    def inventory_check(network):
        inventory_checks.append(network.identity)
        world.events.append(("inventory-check", len(inventory_checks)))
        if len(inventory_checks) == 1:
            raise TransportRetryError("another VPC inventory is still pending")
        return network

    worker.inventory_check = inventory_check
    publish = worker._publish

    def stop_when_ready(state, cause=None, **options):
        publish(state, cause, **options)
        if state == "ready":
            worker.stop.set()

    monkeypatch.setattr(worker, "_publish", stop_when_ready)
    worker.start()
    worker._thread.join(5)
    assert not worker._thread.is_alive()
    worker.close()

    assert aws_inventory_checks == ["network-a"] * 3
    assert inventory_checks == ["network-a", "network-a"]
    assert world.events.index(("inventory-check", 2)) < world.events.index(("transport-start", 1))
    assert world.events.index(("transport-start", 1)) < world.events.index(("forward-add", 1, 3))


def test_terminal_inventory_failure_releases_shared_retry_gate(monkeypatch):
    world = _worker_world(monkeypatch)
    worker = world.worker
    released = []
    worker.inventory_terminal = released.append
    monkeypatch.setattr(
        "stelvio.tunnel.supervisor.resolve_vpc_network",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("ownership changed")),
    )
    worker.start()
    worker._thread.join(5)
    assert not worker._thread.is_alive()
    worker.close()
    assert released == ["network-a"]
    assert worker.status.state == "failed"
    assert not any(event[0] == "forward-add" for event in world.events)


def test_uncertain_configure_is_replayed_only_after_transport_fence(monkeypatch):
    world = _worker_world(monkeypatch, configure_uncertain=True)
    worker = world.worker
    worker.start()
    worker._thread.join(5)
    assert not worker._thread.is_alive()
    worker.close()

    configurations = [e for e in world.events if e[0] == "host-configure"]
    assert len(configurations) == 2
    assert configurations[0][1] == configurations[1][1]
    configure_indices = [
        index for index, event in enumerate(world.events) if event[0] == "host-configure"
    ]
    assert world.events.index(("transport-close", 1)) < configure_indices[1]
    assert any(e[:3] == ("host-remove", worker.unit, 2) for e in world.events)
    assert worker.status.state == "failed"
    assert worker.status.dns_retained


def test_local_artifact_failure_is_terminal_and_does_not_claim_readiness(monkeypatch):
    world = _worker_world(monkeypatch)
    world.transport.start = lambda: (_ for _ in ()).throw(
        TransportArtifactError("Private AWS plugin is missing")
    )
    worker = world.worker
    worker.start()
    worker._thread.join(5)
    assert not worker._thread.is_alive()
    worker.close()
    assert worker.status.state == "failed"
    assert worker.status.cause == "local-artifact"
    assert not any(event[0] == "forward-add" for event in world.events)
    assert not any(event[0] == "host-configure" for event in world.events)


def test_uncertain_transport_cleanup_retains_active_helper_generation(monkeypatch):
    world = _worker_world(monkeypatch, close_uncertain=True)
    worker = world.worker
    publish = worker._publish

    def stop_when_ready(state, cause=None, **options):
        publish(state, cause, **options)
        if state == "ready":
            worker.stop.set()

    monkeypatch.setattr(worker, "_publish", stop_when_ready)
    worker.start()
    worker._thread.join(5)
    assert not worker._thread.is_alive()
    with raises(RuntimeError, match="cleanup incomplete"):
        worker.close()
    assert world.lease.unit["status"] == HelperUnitStatus.ACTIVE
    assert world.lease.unit["generation"] == 1
    assert not any(event[0] == "host-remove" for event in world.events)
    assert worker.status.cause == "cleanup-incomplete"


def test_successful_socks_probe_cannot_mask_failed_direct_path():
    worker = object.__new__(VpcWorker)
    worker._dns_generation = 1
    worker._host_generation = 1
    worker._dns_revision_current = 7
    calls = []

    def dns(operation, **options):
        calls.append((operation, options))
        if options.get("direct"):
            raise ConnectionError("host path unavailable")
        return {}

    worker._dns = dns
    worker.lease = SimpleNamespace(status=lambda: (_ for _ in ()).throw(AssertionError()))
    with raises(ConnectionError, match="host path unavailable"):
        worker._probe(7)
    assert calls == [("probe", {}), ("probe", {"direct": True})]
