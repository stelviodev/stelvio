"""Readiness needs a matching payload reply, not an optimistic local TCP handshake."""

from types import SimpleNamespace

from pytest import mark, raises

from stelvio.tunnel.helper_client import HelperError, HelperStatus, HelperUnitStatus
from stelvio.tunnel.helper_protocol import ResolverEndpoint
from stelvio.tunnel.supervisor import VpcWorker


@mark.parametrize(
    "terminal",
    [
        False,
        True,
        HelperStatus.UNCERTAIN,
        "configure-uncertain",
        "configure-unfenced",
        "foreign-generation",
    ],
)
def test_worker_retries_then_disposes_only_its_owned_connection(  # noqa: C901, PLR0915 - explicit lifecycle boundary fixture
    monkeypatch, terminal
):
    """Exercise the worker across Go DNS, transport and host boundary fakes."""
    from ipaddress import IPv4Network
    from threading import Event

    from stelvio.tunnel.transport import TransportAuthorizationError

    events = []
    published = []
    configurations = []
    dns_state = {"rejected": False, "observed": False}
    network = SimpleNamespace(
        identity="network-a",
        cidrs=(IPv4Network("10.254.0.0/16"),),
        dns_domains=("private.example.com",),
        vpc_id="vpc-owned",
    )

    class Transport:
        attempt = 0
        probes = 0

        def start(self):
            self.attempt += 1
            self.probes = 0
            events.append(("start", self.attempt))
            if self.attempt == 2:
                assert dns_state["rejected"]
            return SimpleNamespace(cidrs=network.cidrs, proxy_port=32123)

        def healthy(self):
            self.probes += 1
            if self.probes == 2:
                if self.attempt == 1:
                    return False
                if terminal is True:
                    raise TransportAuthorizationError("revoked test identity")
            return True

        def close(self):
            events.append(("close", self.attempt))
            if terminal == "configure-unfenced":
                raise RuntimeError("Cannot certify physical stop")

    transport = Transport()
    monkeypatch.setattr("stelvio.tunnel.supervisor.SshTransport", lambda *a, **kw: transport)
    monkeypatch.setattr("stelvio.tunnel.supervisor.resolve_vpc_network", lambda *a: network)
    monkeypatch.setattr("stelvio.tunnel.supervisor.HEALTH_SECONDS", 0.001)

    def configure(**kw):
        configurations.append(kw.copy())
        events.append(("configure", kw["generation"]))
        if terminal == "configure-uncertain" and len(configurations) == 2:
            assert dns_state["rejected"]
            assert events.index(("remove", 1)) < events.index(("close", 1)) < len(events) - 1
        if (
            terminal in ("configure-uncertain", "configure-unfenced", "foreign-generation")
            and events.count(("configure", 1)) == 1
        ):
            raise HelperError(
                "Native helper request failed: uncertain", status=HelperStatus.UNCERTAIN
            )

    lease = SimpleNamespace(
        configure=configure,
        remove=lambda **kw: events.append(("retain", kw)),
    )

    def configure_dns(unit, attempt, revision, network, claims):
        dns_state["rejected"] = False
        events.append(("dns-configure", attempt))
        return 32124

    def dns(operation, *identity, **options):
        events.append(("dns-" + operation, identity[1]))
        if operation == "reject":
            dns_state["rejected"] = True
        return {"observed": dns_state["observed"]}

    forwarding = SimpleNamespace(
        add=lambda unit, generation, attempt, *a: events.append(("add", attempt)),
        remove=lambda unit, generation, attempt: events.append(("remove", attempt)),
        configure_dns=configure_dns,
        dns=dns,
        healthy=lambda: True,
    )
    discovery = SimpleNamespace(
        manifest=SimpleNamespace(resources=()),
        begin=lambda *a: None,
        refresh=lambda *a: None,
    )
    worker = VpcWorker(
        SimpleNamespace(app="app", environment="dev", session_id="session"),
        network,
        SimpleNamespace(client=lambda *a, **kw: None),
        lease,
        forwarding,
        discovery,
    )

    fault_injected = Event()

    def inspect():
        if (
            terminal == HelperStatus.UNCERTAIN
            and transport.attempt == 2
            and transport.probes
            and not fault_injected.is_set()
        ):
            fault_injected.set()
            raise HelperError("Native helper request failed: uncertain", status=terminal)
        return SimpleNamespace(
            owned=True,
            uncertain=False,
            units=(
                SimpleNamespace(
                    unit=worker.unit,
                    generation=99 if terminal == "foreign-generation" else 1,
                    status=HelperUnitStatus.ACTIVE,
                ),
            ),
        )

    lease.inspect = inspect

    def os_query(arguments, **options):
        dns_state["observed"] = True
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr("stelvio.tunnel.supervisor.subprocess.run", os_query)
    publish = worker._publish

    def observe(state, cause=None, **kw):
        publish(state, cause, **kw)
        published.append(worker.status)
        if state == "ready" and worker.status.attempt == 2 and not terminal:
            worker.stop.set()

    monkeypatch.setattr(worker, "_publish", observe)
    worker.start()
    try:
        worker._thread.join(5)
        assert not worker._thread.is_alive(), "Worker did not finish its bounded fixture"
    finally:
        if terminal in ("configure-unfenced", "foreign-generation"):
            with raises(RuntimeError, match="cleanup incomplete"):
                worker.close()
        else:
            worker.close()
    ready = [(s.state, s.attempt) for s in published if s.state == "ready"]
    if terminal in ("configure-uncertain", "configure-unfenced", "foreign-generation"):
        assert ready == []
        expected_count = 2 if terminal == "configure-uncertain" else 1
        assert events.count(("configure", 1)) == expected_count
        expected_grant = {
            "unit": worker.unit,
            "generation": 1,
            "vpc_id": "vpc-owned",
            "cidrs": ("10.254.0.0/16",),
            "resolvers": (ResolverEndpoint("private.example.com", 32124),),
        }
        assert configurations == [expected_grant] * expected_count
        assert ("start", 2) not in events
        assert ("remove", 1) in events
    else:
        assert ready == [("ready", 1), ("ready", 2)]
        assert (
            events.index(("remove", 1)) < events.index(("close", 1)) < events.index(("start", 2))
        )
        assert ("remove", 2) in events
    assert not any(event[0] == "aws-cleanup" for event in events)
    assert dns_state["rejected"]
    if terminal in ("configure-unfenced", "foreign-generation"):
        assert worker.status.cause == "cleanup-incomplete"
        assert not worker.status.dns_retained
        assert not any(event[0] == "retain" for event in events)
    elif terminal:
        assert worker.status.state == "failed"
        expected = (
            "host-uncertain"
            if terminal in (HelperStatus.UNCERTAIN, "configure-uncertain")
            else "identity-or-configuration"
        )
        assert worker.status.cause == expected
        assert worker.status.dns_retained
        assert ("retain", {"unit": worker.unit, "generation": 2, "keep_dns": True}) in events
    else:
        assert worker.status.state == "stopped"
        assert not any(event[0] == "retain" for event in events)


def test_successful_socks_probe_cannot_mask_failed_host_path():
    from unittest.mock import Mock

    worker = object.__new__(VpcWorker)
    worker._dns_generation = 1
    calls = []

    def dns(operation, **options):
        calls.append((operation, options))
        if options.get("direct"):
            raise ConnectionError("host path unavailable")
        return {}

    worker._dns = dns
    worker.lease = SimpleNamespace(inspect=Mock(side_effect=AssertionError("not ready")))
    with raises(ConnectionError, match="host path unavailable"):
        worker._probe(1)
    assert calls == [("probe", {}), ("probe", {"direct": True})]
