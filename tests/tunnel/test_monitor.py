"""Monitor accounting, session lifecycle, and real headless terminal interaction."""

import asyncio
import os
import subprocess
import sys
from copy import deepcopy
from uuid import uuid4

import pytest
from click.testing import CliRunner

from stelvio.cli.tunnel import tunnel
from stelvio.tunnel.diagnostics.protocol import observation
from stelvio.tunnel.monitor.client import MonitorClient
from stelvio.tunnel.monitor.model import DROP_FIELDS, Dashboard, validate


def sample(session_id=None, *, tick=1, count=100):
    session_id = session_id or str(uuid4())
    vpcs = [
        {
            "identity": name,
            "unit": f"{index:08x}",
            "state": "ready",
            "generation": 7,
            "attempt": 2,
            "dns_revision": 4,
            "dns_port": 53000 + index,
            "ranges": [f"10.{index}.0.0/24"],
            "connected_seconds": 754,
            "checks": {"result": "passed"},
            "resources": [],
        }
        for index, name in enumerate(("orders-main", "analytics"), 1)
    ]
    return {
        "epoch": "runtime",
        "version": 1,
        "sample_monotonic_ns": tick * 10**9,
        "session": {
            "session_id": session_id,
            "app": "orders",
            "environment": "dev",
            "forwarder_pid": 42,
            "vpcs": vpcs,
            "records": [
                {
                    "kind": "vpc",
                    "identity": item["identity"],
                    "vpc_id": "vpc-" + item["unit"],
                    "account": "123456789012",
                    "region": "us-east-1",
                }
                for item in vpcs
            ],
        },
        "sources": {
            "helper": observation(
                {
                    "epoch": "helper",
                    "session_id": session_id,
                    "sample_monotonic_ns": tick * 10**9,
                    "details_available": True,
                    "units": [
                        {
                            "unit": vpc["unit"],
                            "generation": 7,
                            "active": True,
                            "interface_name": "utun8",
                            "counters": {
                                "host_delivered_bytes": count,
                                "return_delivered_bytes": count * 2,
                                **dict.fromkeys(DROP_FIELDS, 0),
                            },
                        }
                        for vpc in vpcs
                    ],
                }
            ),
            "forwarder": observation(
                {
                    "epoch": "forwarder",
                    "pid": 42,
                    "sample_monotonic_ns": tick * 10**9,
                    "units": [
                        {
                            "unit": vpc["unit"],
                            "generation": 7,
                            "attempt": 2,
                            "dns_revision": 4,
                            "dns_port": vpc["dns_port"],
                            "dns_upstream_enabled": True,
                            "tcp": {"active": 3, "queue_drops": 0},
                        }
                        for vpc in vpcs
                    ],
                }
            ),
        },
        "events": {
            "entries": [
                {
                    "sequence": 1,
                    "timestamp": "2026-10-24T14:32:13+00:00",
                    "code": "vpc-ready",
                    "detail": "[red]literal[/red]",
                }
            ],
            "newest_sequence": 1,
            "oldest_sequence": 1,
            "gap": False,
        },
    }


def test_rates_cached_samples_gaps_and_reconnect():
    value = sample()
    model = Dashboard()
    model.update(value, 1)
    assert model.total("upload") is None
    second = sample(value["session"]["session_id"], tick=2, count=1124)
    model.update(second, 2)
    assert model.total("upload") == 2048
    assert model.total("download") == 4096
    assert model.total("streams") == 6
    assert model.state == "READY"
    model.update(second, 3)
    assert model.total("upload") == 2048
    assert len(model.events) == 1
    model.missing(5)
    assert model.state == "Disconnected"
    assert model.history[-1] == (5, None, None)
    assert model.history[-2] == (4, None, None)
    model.update(second, 6)
    assert model.total("upload") is None
    assert len(model.events) == 1


@pytest.mark.parametrize(
    "change", ["epoch", "generation", "regression", "session", "stale", "missing"]
)
def test_counter_reset_and_missing_sources(change):
    value = sample()
    model = Dashboard()
    model.update(value, 1)
    second = sample(value["session"]["session_id"], tick=2, count=200)
    if change == "epoch":
        second["sources"]["helper"]["data"]["epoch"] = "replacement"
    elif change == "generation":
        second["session"]["vpcs"][0]["generation"] += 1
    elif change == "regression":
        second["sources"]["helper"]["data"]["units"][0]["counters"]["host_delivered_bytes"] = 1
    elif change == "session":
        second["sources"]["helper"]["data"]["session_id"] = str(uuid4())
    elif change == "stale":
        second["sources"]["helper"]["stale"] = True
    else:
        second["sources"].pop("helper")
    model.update(second, 2)
    assert model.total("upload") is None
    assert model.rows[0]["upload"] is None
    if change not in {"epoch", "regression"}:
        assert model.state != "READY"


@pytest.mark.parametrize("field", ["generation", "attempt", "dns_revision"])
def test_forwarder_identity_mismatch_is_not_healthy(field):
    value = sample()
    value["sources"]["forwarder"]["data"]["units"][0][field] += 1
    model = Dashboard()
    model.update(value, 1)
    assert model.total("streams") is None
    assert not model.rows[0]["dns_healthy"]
    assert model.state != "READY"


def test_event_gaps_and_bounded_history():
    value = sample()
    value["events"].update(gap=True, oldest_sequence=300, newest_sequence=300)
    value["events"]["entries"][0]["sequence"] = 300
    model = Dashboard()
    model.update(value, 1)
    assert model.events[0]["code"] == "Event history gap"
    for tick in range(2, 100):
        model.update(value, tick)
    assert len(model.history) == 60
    assert len(model.events) == 2


@pytest.mark.parametrize("mutation", ["bool", "huge", "duplicate", "records", "counter"])
def test_malformed_snapshots(mutation):
    value = sample()
    if mutation == "bool":
        value["session"]["vpcs"][0]["generation"] = True
    elif mutation == "huge":
        value["sources"]["helper"]["data"]["sample_monotonic_ns"] = 10**1000
    elif mutation == "duplicate":
        value["session"]["vpcs"].append(value["session"]["vpcs"][0])
    elif mutation == "records":
        value["session"]["records"] = ["bad"]
    else:
        value["sources"]["helper"]["data"]["units"][0]["counters"] = "bad"
    with pytest.raises((ValueError, TypeError)):
        validate(value)


def test_session_discovery_selection_and_replacement(monkeypatch, tmp_path):
    clock = [1]
    first, second = sample(), sample()
    values = {tmp_path / f"{item['session']['session_id']}.sock": item for item in (first, second)}
    monkeypatch.setattr("stelvio.tunnel.monitor.client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("stelvio.tunnel.monitor.client.runtime.discover", lambda: list(values))
    monkeypatch.setattr(
        "stelvio.tunnel.monitor.client.runtime.snapshot",
        lambda path, *a, **kw: deepcopy(values[path]),
    )
    client = MonitorClient()
    client.poll()
    clock[0] += 2
    result = client.poll()
    assert len(result["sessions"]) == 2
    assert result["snapshot"] is None
    chosen = first["session"]["session_id"]
    assert client.poll(chosen)["snapshot"]["session"]["session_id"] == chosen
    # A surviving session does not get selected merely because it has the same identity.
    assert client.poll()["snapshot"]["session"]["session_id"] == chosen
    values.clear()
    clock[0] += 2
    assert client.poll()["snapshot"] is None
    replacement = sample()
    values[tmp_path / f"{replacement['session']['session_id']}.sock"] = replacement
    clock[0] += 2
    assert (
        client.poll()["snapshot"]["session"]["session_id"] == replacement["session"]["session_id"]
    )
    pinned = MonitorClient(chosen)
    assert pinned.poll()["snapshot"] is None


def test_monitor_capability_negotiation(monkeypatch, tmp_path):
    value = sample()
    path = tmp_path / f"{value['session']['session_id']}.sock"
    calls = []
    monkeypatch.setattr("stelvio.tunnel.monitor.client.runtime.discover", lambda: [path])

    def read(path, after_sequence=0, **options):
        calls.append(options["resource_checks"])
        return deepcopy(value)

    monkeypatch.setattr("stelvio.tunnel.monitor.client.runtime.snapshot", read)
    client = MonitorClient()
    client.poll()
    client.poll()
    assert not any(calls)
    value["capabilities"] = ["resource_checks"]
    client.poll()
    client.poll()
    assert calls[-1] is True


def test_noninteractive_and_unsupported_commands_do_not_load_textual(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(os, "geteuid", lambda: 501)
    monkeypatch.setattr(os, "getuid", lambda: 501)
    result = CliRunner().invoke(tunnel, ["monitor"])
    assert result.exit_code == 1
    assert "diagnose --json" in result.output
    monkeypatch.setattr(sys, "platform", "linux")
    result = CliRunner().invoke(tunnel, ["monitor"])
    assert "requires macOS" in result.output
    result = subprocess.run(  # noqa: S603 - isolated import check
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; from stelvio.cli.tunnel import tunnel; "
            "assert 'textual' not in sys.modules",
        ],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_textual_navigation_resize_refresh_disconnect_and_quit():
    from textual.widgets import DataTable, Static

    from stelvio.tunnel.monitor.app import TunnelMonitor

    class Client:
        pinned = None
        value = sample()
        count = 0

        def poll(self, requested=None):
            self.count += 1
            return {
                "snapshot": deepcopy(self.value),
                "sessions": [self.value] if self.value else [],
                "error": None,
            }

    async def exercise():
        client = Client()
        app = TunnelMonitor(client=client)
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert app.dashboard.state == "READY"
            assert app.query_one(DataTable).row_count == 2
            await pilot.press("down")
            assert app.selected_unit == "analytics"
            await pilot.press("r")
            await pilot.pause()
            assert client.count >= 2
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert not app.screen.has_class("small")
            assert "[red]literal[/red]" in str(app.query_one("#events", Static).render())
            client.value = None
            await pilot.press("r")
            await pilot.pause()
            assert app.dashboard.state == "Disconnected"
            await pilot.resize_terminal(50, 15)
            await pilot.pause()
            assert app.screen.has_class("small")
            await pilot.press("q")
        assert not app.is_running

    asyncio.run(exercise())


def test_ambiguous_replacement_waits_for_explicit_selection(monkeypatch, tmp_path):
    clock = [1]
    first = sample()
    values = {tmp_path / f"{first['session']['session_id']}.sock": first}
    monkeypatch.setattr("stelvio.tunnel.monitor.client.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("stelvio.tunnel.monitor.client.runtime.discover", lambda: list(values))
    monkeypatch.setattr(
        "stelvio.tunnel.monitor.client.runtime.snapshot",
        lambda path, *a, **kw: deepcopy(values[path]),
    )
    client = MonitorClient()
    assert client.poll()["snapshot"] is not None
    values.clear()
    replacements = [sample(), sample()]
    values.update(
        {tmp_path / f"{item['session']['session_id']}.sock": item for item in replacements}
    )
    for _ in range(2):
        clock[0] += 2
        assert client.poll()["snapshot"] is None
    chosen = replacements[1]["session"]["session_id"]
    assert client.poll(chosen)["snapshot"]["session"]["session_id"] == chosen
