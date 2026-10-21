"""Generation-fenced VPC reconnects and readiness with isolated adapters."""

from ipaddress import IPv4Network
from types import SimpleNamespace

from pytest import mark, raises

from stelvio.tunnel.evidence import record_transport_event
from stelvio.tunnel.helper_client import (
    HelperError,
    HelperRouteConflict,
    HelperStatus,
    HelperUnitStatus,
)
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
        route_cidrs=(IPv4Network("10.254.16.0/20"), IPv4Network("10.254.64.0/20")),
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
            assert configured_network.route_cidrs == network.route_cidrs
            events.append(("forward-add", generation, attempt))

        def configure_dns(  # noqa: PLR0913 - mirrors the forwarder protocol signature
            self,
            unit,
            generation,
            attempt,
            revision,
            configured_network,
            names,
            *,
            probe_domain=None,
        ):
            if (generation, attempt) != (self.host_generation, self.attempt):
                raise ValueError("stale DNS configuration")
            if revision <= self.revision:
                raise ValueError("stale DNS revision")
            assert probe_domain == worker._probe_domain
            assert probe_domain in names
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
    assert all(
        entry[1]["cidrs"] == ("10.254.16.0/20", "10.254.64.0/20") for entry in configurations
    )
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


def test_successful_socks_probe_cannot_mask_failed_direct_resource_path(monkeypatch):
    world = _worker_world(monkeypatch)
    worker = world.worker
    worker._dns_generation = worker._host_generation = 1
    worker._dns_revision_current = 7
    calls = []
    world.lease.unit = {"unit": worker.unit, "generation": 1, "status": HelperUnitStatus.ACTIVE}

    def dns(operation, **options):
        calls.append((operation, options))
        if options.get("direct"):
            assert options["name"] == "db.private.example.com"
            raise ConnectionError("host resource path unavailable")
        return {}

    worker._dns = dns
    with raises(ConnectionError, match="host resource path unavailable"):
        worker._probe(7)
    assert calls == [
        ("probe", {}),
        ("probe", {"name": "db.private.example.com", "port": 27017}),
        ("probe", {"name": "db.private.example.com", "port": 27017, "direct": True}),
    ]


def test_changed_ssh_key_is_terminal_before_forwarding(monkeypatch):
    from stelvio.tunnel.transport import ServerIdentityError

    world = _worker_world(monkeypatch)
    detail = (
        "SSH host key changed for aws/123456789012/us-east-1/i-12345678: "
        "expected SHA256:old, presented SHA256:new."
    )

    def changed():
        world.events.append(("changed-host-key",))
        raise ServerIdentityError(detail)

    world.transport.start = changed
    world.worker.start()
    world.worker._thread.join(5)
    assert not world.worker._thread.is_alive()
    world.worker.close()
    assert world.worker.status.state == "failed"
    assert world.worker.status.cause == "ssh-host-key"
    assert world.worker.status.detail == detail
    assert world.events.count(("changed-host-key",)) == 1
    assert not any(event[0] == "forward-add" for event in world.events)
    assert not any(event[0] == "host-configure" for event in world.events)


def test_key_change_during_reconnect_retains_dns_and_stops_retry(monkeypatch):
    from stelvio.tunnel.transport import ServerIdentityError

    world = _worker_world(monkeypatch, fail_first=True)
    original_start = world.transport.start
    published = []
    original_publish = world.worker._publish

    def start():
        connector = original_start()
        if world.transport.attempt == 2:
            assert world.dns_state["rejected"]
            raise ServerIdentityError("SSH host key changed for i-12345678; trust reset required")
        return connector

    def publish(state, cause=None, **options):
        original_publish(state, cause, **options)
        published.append(world.worker.status)

    monkeypatch.setattr(world.transport, "start", start)
    monkeypatch.setattr(world.worker, "_publish", publish)
    world.worker.start()
    world.worker._thread.join(5)
    assert not world.worker._thread.is_alive()
    world.worker.close()
    assert world.transport.attempt == 2
    assert any(status.state == "ready" and status.attempt == 1 for status in published)
    assert not any(status.state == "ready" and status.attempt > 1 for status in published)
    assert world.worker.status.cause == "ssh-host-key"
    assert world.worker.status.dns_retained
    assert world.dns_state["rejected"]
    assert world.lease.unit["status"] == HelperUnitStatus.RETAINED
    assert [event[1:3] for event in world.events if event[0] == "forward-add"] == [(1, 1)]
    assert world.forwarding.port == world.worker._dns_port
    rejection = next(i for i, event in enumerate(world.events) if event[0] == "dns-reject")
    removal = next(i for i, event in enumerate(world.events) if event[0] == "forward-remove")
    assert rejection < removal


@mark.parametrize("routes", [(), (IPv4Network("10.254.0.0/16"),)])
def test_missing_or_changed_destination_ranges_fail_before_transport(monkeypatch, routes):
    world = _worker_world(monkeypatch)
    world.worker.network.route_cidrs = routes
    if not routes:
        world.worker._route_cidrs = ()
    world.worker.start()
    world.worker._thread.join(5)
    assert not world.worker._thread.is_alive()
    world.worker.close()
    assert world.worker.status.cause == "identity-or-configuration"
    assert not any(
        e[0] in {"transport-start", "forward-add", "host-configure"} for e in world.events
    )


def test_member_refresh_keeps_subnet_routes_and_listener_port(monkeypatch):
    world = _worker_world(monkeypatch)
    worker = world.worker
    worker._host_generation = worker._attempt = 1
    world.forwarding.add(worker.unit, 1, 1, worker.network, 32123)
    worker._configure(worker._view())
    original = world.lease.unit["request"]
    worker._claims += ("member.private.example.com",)
    worker._configure(worker._view())
    refreshed = world.lease.unit["request"]
    assert refreshed["cidrs"] == original["cidrs"] == ("10.254.16.0/20", "10.254.64.0/20")
    assert refreshed["generation"] == original["generation"] == 1
    assert refreshed["resolvers"][: len(original["resolvers"])] == original["resolvers"]
    assert all(r.port == world.forwarding.port for r in refreshed["resolvers"])


@mark.parametrize("cleanup_failure", [False, True])
def test_route_conflict_is_terminal_and_reports_only_decoded_facts(monkeypatch, cleanup_failure):
    from stelvio.tunnel.supervisor import statuses

    world = _worker_world(monkeypatch, close_uncertain=cleanup_failure)
    configure = world.lease.configure

    def conflict(**request):
        configure(**request)
        world.lease.configure = configure
        raise HelperError(
            "untrusted exception text SECRET",
            status=HelperStatus.UNCERTAIN,
            configure_stage=20,
            route_conflict=HelperRouteConflict("10.254.16.0/20", "10.254.17.0/24", 7),
        )

    world.lease.configure = conflict
    world.worker.start()
    world.worker._thread.join(5)
    assert not world.worker._thread.is_alive()
    if cleanup_failure:
        with raises(RuntimeError, match="cleanup incomplete"):
            world.worker.close()
    else:
        world.worker.close()
    status = statuses((world.worker,))[0]
    assert status["state"] == "failed"
    assert status["cause"] == (
        "cleanup-incomplete" if cleanup_failure else "host-uncertain-configure-20"
    )
    assert "10.254.16.0/20" in status["detail"]
    assert "10.254.17.0/24" in status["detail"]
    assert "interface index 7" in status["detail"]
    assert "SECRET" not in status["detail"]
    assert world.transport.attempt == 1


@mark.parametrize("domains", [(), ("private.example.com",)])
def test_probe_scope_is_owned_stable_and_separate_from_resource_names(monkeypatch, domains):
    world = _worker_world(monkeypatch)
    worker = world.worker
    worker.network.dns_domains = domains
    worker._host_generation = worker._attempt = 1
    world.forwarding.add(worker.unit, 1, 1, worker.network, 32123)
    worker._configure(worker._view())
    probe = worker._probe_domain
    assert probe == f"probe-session-{worker.unit}.stelvio.test"
    assert worker._claims == (*domains, probe, "db.private.example.com")
    worker._probe(worker._dns_revision_current)
    worker._probe(worker._dns_revision_current)
    arms = [e[-1]["name"] for e in world.events if e[0] == "dns-arm"]
    assert len(set(arms)) == 2
    assert all(name.endswith("." + probe) for name in arms)
    assert all(not name.endswith(".db.private.example.com") for name in arms)
    worker._claims += ("member.private.example.com",)
    worker._configure(worker._view())
    assert worker._probe_domain == probe
    assert worker._claims.count(probe) == 1


@mark.parametrize(("resource_count", "allowed"), [(63, True), (64, False)])
def test_probe_scope_counts_toward_name_limit(monkeypatch, resource_count, allowed):
    from stelvio.tunnel.transport import TransportAuthorizationError

    world = _worker_world(monkeypatch)
    worker = world.worker
    worker.network.dns_domains = ()
    resource = world.worker.discovery.manifest.resources[0]
    from dataclasses import replace

    worker.discovery.manifest = NetworkManifest(
        (),
        (
            replace(
                resource, hostnames=tuple(f"db-{i}.example.com" for i in range(resource_count))
            ),
        ),
        (),
    )
    if allowed:
        worker._view()
        assert len(worker._claims) == 64
    else:
        with raises(TransportAuthorizationError, match="domain bound"):
            worker._view()
        assert not any(e[0] == "host-configure" for e in world.events)
