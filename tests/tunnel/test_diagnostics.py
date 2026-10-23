"""Real diagnostics IPC, sampling, source isolation, and read-only CLI reports."""

import array
import io
import json
import os
import socket
import struct
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from click.testing import CliRunner
from rich.console import Console

from stelvio.cli.tunnel import tunnel
from stelvio.tunnel.diagnostics import protocol, report, runtime


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    temporary = tempfile.TemporaryDirectory(prefix="sd-", dir="/private/tmp")
    path = Path(temporary.name) / "d"
    monkeypatch.setattr(runtime, "directory", lambda: path)
    events = runtime.Events()
    service = runtime.RuntimeDiagnostics(
        str(uuid4()),
        lambda: {"session_id": "session", "vpcs": []},
        {"helper": lambda: {"counter": 0}},
        events,
    )
    service.start()
    yield service, events
    service.close()
    temporary.cleanup()


def test_runtime_socket_snapshot_discovery_and_event_gap(endpoint):
    service, events = endpoint
    for _ in range(260):
        events.append("vpc-ready", "00000001")
    assert runtime.discover() == [service.path]
    value = runtime.snapshot(service.path)
    assert value["sources"]["helper"]["data"] == {"counter": 0}
    assert value["events"]["gap"] is True
    assert len(value["events"]["entries"]) == 256
    assert runtime.snapshot(service.path, 260)["events"]["entries"] == []
    service.close()
    assert runtime.discover() == []


@pytest.mark.parametrize("mutation", ["unsafe-directory", "unsafe-socket", "symlink"])
def test_discovery_rejects_unsafe_paths(endpoint, mutation):
    service, _ = endpoint
    if mutation == "unsafe-directory":
        service.path.parent.chmod(0o755)
        with pytest.raises(ValueError, match="directory"):
            runtime.discover()
    elif mutation == "unsafe-socket":
        service.path.chmod(0o666)
        assert runtime.discover() == []
    else:
        link = service.path.parent / "foreign.sock"
        link.symlink_to(service.path)
        assert runtime.discover() == [service.path]


def test_close_preserves_replaced_endpoint(endpoint):
    service, _ = endpoint
    service.path.unlink()
    service.path.write_text("foreign")
    service.close()
    assert service.path.read_text() == "foreign"


def test_sampler_single_flight_partial_failure_and_copy():
    calls = []
    sampler = runtime.Sampler({"helper": lambda: calls.append(1) or {"bytes": 12}})
    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(lambda _: sampler.collect(), range(4)))
    assert calls == [1]
    values[0]["helper"]["data"]["bytes"] = 99
    assert values[1]["helper"]["data"]["bytes"] == 12
    sampler.sources["helper"] = lambda: (_ for _ in ()).throw(TimeoutError("secret"))
    sampler.attempts["helper"] -= 2
    sampler.observed["helper"] -= 6
    result = sampler.collect()["helper"]
    assert result["data"] == {"bytes": 12}
    assert result["stale"] is True
    assert result["error"] == {"code": "TimeoutError"}
    assert "secret" not in json.dumps(result)


@pytest.mark.parametrize("wire", [b'{"version":1,"version":1}', b'{"x":NaN}', b"[]", b"{"])
def test_decode_rejects_invalid_objects(wire):
    with pytest.raises((TypeError, ValueError)):
        protocol.decode(wire)


@pytest.mark.parametrize("value", [True, -1, "1", None])
def test_invalid_event_sequence(value):
    with pytest.raises(ValueError, match="sequence"):
        protocol.request(value)


def test_framing_rejects_oversize_and_closes_descriptors():
    with _Pair() as pair:
        first, second = pair
        second.sendall(struct.pack("!I", protocol.MAX_MESSAGE + 1))
        with pytest.raises(ValueError, match="bound"):
            protocol.receive(first)
    with _Pair() as (first, second):
        descriptor = os.open(os.devnull, os.O_RDONLY)
        try:
            second.sendmsg(
                [b"1234"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [descriptor]))]
            )
            with pytest.raises(ValueError, match="Descriptors"):
                protocol.receive(first)
        finally:
            os.close(descriptor)


class _Pair:
    def __enter__(self):
        self.pair = socket.socketpair()
        for connection in self.pair:
            connection.settimeout(0.5)
        return self.pair

    def __exit__(self, *_):
        for connection in self.pair:
            connection.close()


def test_server_rejects_mutations_and_wrong_peers(endpoint, monkeypatch):
    service, _ = endpoint
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        connection.connect(str(service.path))
        protocol.send(connection, dict(protocol.request(), operation="stop"))
        assert connection.recv(1) == b""
    monkeypatch.setattr(runtime, "peer_uid", lambda _: os.geteuid() + 1)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(1)
        connection.connect(str(service.path))
        assert connection.recv(1) == b""


def test_report_partial_sources_valid_json_and_literal_rich(monkeypatch):
    monkeypatch.setattr(report.sys, "platform", "unsupported")
    monkeypatch.setattr(report, "artifacts", lambda: (_ for _ in ()).throw(OSError("secret")))
    monkeypatch.setattr(report, "helper", lambda: protocol.observation(status="unavailable"))
    monkeypatch.setattr(report, "sessions", lambda: protocol.observation({"sessions": []}))
    value = report.collect()
    assert value["sections"]["artifacts"]["error"]["code"] == "OSError"
    assert "secret" not in json.dumps(value)
    value["sections"]["literal"] = protocol.observation("[bold]literal[/bold]")
    output = io.StringIO()
    report.render(value, Console(file=output, width=100, color_system=None))
    assert "[bold]literal[/bold]" in output.getvalue()
    monkeypatch.setattr(report, "collect", lambda: value)
    result = CliRunner().invoke(tunnel, ["diagnose", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == value
    assert CliRunner().invoke(tunnel, ["diagnose", "--post"]).exit_code == 2


def test_command_bounds_output_and_stops_stalled_subprocess():
    value = report.command([sys.executable, "-c", "print('a' * 200000)"], time.monotonic() + 3)
    assert value["data"]["truncated"] is True
    assert len(value["data"]["output"]) == report.OUTPUT_LIMIT
    started = time.monotonic()
    value = report.command([sys.executable, "-c", "import time; time.sleep(10)"], started + 0.2)
    assert value["error"]["code"] == "command-timeout"
    assert time.monotonic() - started < 2


def test_system_sanitizers_exclude_environment_and_proxy_secrets():
    value = report._sanitize_system(
        "helper_service",
        protocol.observation(
            {"output": "state = running\n AWS_SECRET_ACCESS_KEY = secret\n pid = 42\n"}
        ),
    )
    assert "secret" not in json.dumps(value)
    value = report._sanitize_system(
        "proxies",
        protocol.observation(
            {
                "output": "HTTPEnable : 1\n HTTPProxy : user:password@proxy\n"
                " ProxyAutoConfigURLString : https://user:secret@proxy/pac?token=secret\n"
            }
        ),
    )
    assert "secret" not in json.dumps(value)
    assert "password" not in json.dumps(value)
    assert "HTTPEnable: 1" in value["data"]["output"]


def test_report_size_bound_preserves_errors(monkeypatch):
    monkeypatch.setattr(report, "REPORT_LIMIT", 512)
    value = {"sections": {"one": protocol.observation("x" * 5000)}, "truncated": False}
    assert len(json.dumps(report.bound_report(value))) <= 512
    assert value["truncated"] is True
    assert value["sections"]["one"]["truncated"] is True


def test_artifact_inspection_never_executes_or_creates_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(report.subprocess, "Popen", lambda *_args, **_kw: pytest.fail("executed"))
    path = tmp_path / "artifact"
    path.write_bytes(struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0))
    path.chmod(0o755)
    value = report._artifact(path, "wrong-digest", os.geteuid(), "arm64")
    assert value["error"]["code"] == "artifact-digest"
    path.unlink()
    path.symlink_to(tmp_path / "absent")
    with pytest.raises(OSError, match=r"Too many levels|loop|symbolic link"):
        report._artifact(path, "digest", os.geteuid(), "arm64")


def test_route_findings_expose_existing_vpn_conflicts():
    value = {
        "sections": {
            "routes": protocol.observation(
                {
                    "output": "192.0.2/23 utun7 Uc utun7\n10.254/16 utun7 Uc utun7\n"
                    "default 192.168.1.1 UGSc en0\n"
                }
            )
        },
        "findings": [],
    }
    import ipaddress

    report._route_findings(value, [("session", "unit", ipaddress.IPv4Network("10.254.0.0/16"))])
    assert {item["code"] for item in value["findings"]} == {
        "helper-address-route-present",
        "selected-range-route-overlap",
    }
    assert all(item["interface"] == "utun7" for item in value["findings"])


def test_report_reuses_fresh_matching_runtime_helper_sample(monkeypatch):
    source = protocol.observation({"session_id": "owned", "epoch": "helper", "units": []})
    sections = {
        "sessions": protocol.observation(
            {"sessions": [{"session": {"session_id": "owned"}, "sources": {"helper": source}}]}
        )
    }
    monkeypatch.setattr(report, "helper", lambda: pytest.fail("duplicate helper poll"))
    assert report._helper_observation(sections, time.monotonic() + 2) is source
    source["data"]["session_id"] = "another-owner"
    monkeypatch.setattr(report, "helper", lambda: protocol.observation(status="unavailable"))
    assert report._helper_observation(sections, time.monotonic() + 2)["status"] == "unavailable"
