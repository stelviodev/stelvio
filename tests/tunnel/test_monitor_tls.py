"""Real local TLS checks, bounded runtime scheduling, and passive IPC compatibility."""

import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest

from stelvio.tunnel.diagnostics import protocol, runtime
from stelvio.tunnel.diagnostics.resource_checks import ResourceChecks, check_endpoint, targets


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    directory = tmp_path_factory.mktemp("monitor-tls")
    key, csr, cert, expired = [
        directory / name for name in ("key.pem", "req.pem", "cert.pem", "expired.pem")
    ]

    def openssl(*arguments):
        subprocess.run(  # noqa: S603 - local certificate generator with fixed commands
            [shutil.which("openssl"), *map(str, arguments)],
            check=True,
            capture_output=True,
            timeout=15,
        )

    openssl(
        "req",
        "-new",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        key,
        "-out",
        csr,
        "-subj",
        "/CN=localhost",
    )
    extensions = directory / "extensions.cnf"
    extensions.write_text("subjectAltName=DNS:localhost\nbasicConstraints=critical,CA:TRUE\n")
    openssl(
        "x509",
        "-req",
        "-in",
        csr,
        "-signkey",
        key,
        "-out",
        cert,
        "-days",
        "1",
        "-extfile",
        extensions,
    )
    (directory / "index").write_text("")
    (directory / "serial").write_text("01\n")
    config = directory / "ca.cnf"
    config.write_text(f"""[ca]
default_ca = local
[local]
database = {directory}/index
serial = {directory}/serial
new_certs_dir = {directory}
certificate = {cert}
private_key = {key}
default_md = sha256
policy = names
x509_extensions = extensions
[names]
commonName = supplied
[extensions]
subjectAltName = DNS:localhost
basicConstraints = critical,CA:TRUE
""")
    openssl(
        "ca",
        "-batch",
        "-notext",
        "-config",
        config,
        "-in",
        csr,
        "-out",
        expired,
        "-startdate",
        "20000101000000Z",
        "-enddate",
        "20010101000000Z",
    )
    return key, cert, expired


@contextmanager
def tls_server(key, cert, *, stall=False):
    stop = Event()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.1)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with connection:
                connection.settimeout(3)
                if stall:
                    stop.wait(3)
                else:
                    try:
                        with context.wrap_socket(connection, server_side=True):
                            pass
                    except OSError:
                        pass

    thread = Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=4)


def state(port):
    return {
        "vpcs": [
            {
                "state": "ready",
                "unit": "00000001",
                "generation": 7,
                "attempt": 2,
                "dns_revision": 4,
                "ranges": ["127.0.0.0/8"],
                "resources": [
                    {
                        "identity": "orders",
                        "service": "documentdb",
                        "hostnames": ["localhost"],
                        "ports": [port],
                    }
                ],
            }
        ]
    }


def wait_result(checks, value):
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = checks.collect(value, requested=False)
        if result["observed_at"] is not None:
            return result
        time.sleep(0.02)
    pytest.fail("TLS round did not complete")


def test_real_tls_valid_wrong_hostname_and_untrusted_ca(certificates):
    key, cert, _ = certificates
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cert)
    with tls_server(key, cert) as port:
        target = targets(state(port))[0]
        assert check_endpoint(target, context)["result"] == "passed"
        target["host"] = "127.0.0.1"
        assert check_endpoint(target, context)["error"] == "certificate-verification"
        target["host"] = "localhost"
        assert (
            check_endpoint(target, ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT))["error"]
            == "certificate-verification"
        )


def test_expired_certificate_and_timeout(certificates):
    key, cert, expired = certificates
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(expired)
    with tls_server(key, expired) as port:
        assert (
            check_endpoint(targets(state(port))[0], context)["error"] == "certificate-verification"
        )
    with tls_server(key, cert, stall=True) as port:
        assert check_endpoint(targets(state(port))[0], context)["error"] == "timeout"


def test_real_isolated_round_and_shared_schedule(certificates):
    key, cert, _ = certificates
    checks = ResourceChecks(cert.read_text())
    try:
        with tls_server(key, cert) as port:
            value = state(port)
            # Passive reports cannot schedule network I/O.
            assert checks.collect(value, requested=False)["result"] == "pending"
            assert checks.thread is None
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: checks.collect(value, requested=True), range(4)))
            thread = checks.thread
            result = wait_result(checks, value)
            assert result["result"] == "passed", result
            assert result["checked"] == result["total"] == 1
            assert result["endpoints"][0]["host"] == "localhost"
            checks.collect(value, requested=True)
            assert checks.thread is thread
            assert value["vpcs"][0]["state"] == "ready"
            # A newly discovered member invalidates the complete old result.
            value["vpcs"][0]["resources"][0]["hostnames"].append("new.localhost")
            result = checks.collect(value, requested=False)
            assert result["result"] == "pending"
            assert result["total"] == 2
            assert result["endpoints"] == []
    finally:
        checks.close()


def test_missing_invalid_bundle_partial_members_and_staleness(certificates):
    key, cert, _ = certificates
    missing = ResourceChecks(None)
    value = state(12345)
    assert missing.collect(value, requested=False)["error"] == "missing-ca-bundle"
    invalid = ResourceChecks("not a PEM certificate")
    try:
        invalid.collect(value, requested=True)
        assert wait_result(invalid, value)["result"] == "unavailable"
    finally:
        invalid.close()
    checks = ResourceChecks(cert.read_text())
    try:
        with tls_server(key, cert) as port:
            value = state(port)
            value["vpcs"][0]["resources"][0]["hostnames"].append("127.0.0.1")
            checks.collect(value, requested=True)
            result = wait_result(checks, value)
            assert result["result"] == "failed"
            assert {item["result"] for item in result["endpoints"]} == {"passed", "failed"}
            checks.observed -= 61
            assert checks.collect(value, requested=False)["stale"] is True
            assert value["vpcs"][0]["state"] == "ready"
    finally:
        checks.close()


def test_round_deadline_kills_worker_and_discards_obsolete_generation(monkeypatch):
    from stelvio.tunnel.diagnostics import resource_checks

    original = subprocess.Popen
    processes = []

    def stalled(*args, **kwargs):
        process = original([sys.executable, "-I", "-c", "import time; time.sleep(60)"], **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(resource_checks.subprocess, "Popen", stalled)
    monkeypatch.setattr(resource_checks, "ROUND_SECONDS", 0.3)
    checks = ResourceChecks("test CA")
    value = state(12345)
    try:
        checks.collect(value, requested=True)
        result = wait_result(checks, value)
        assert result["error"] == "round-timeout"
        assert all(process.poll() is not None for process in processes)
        checks.last_started = float("-inf")
        checks.collect(value, requested=True)
        value["vpcs"][0]["generation"] += 1
        checks.collect(value, requested=False)
        checks.thread.join(timeout=2)
        assert checks.value is None
        assert value["vpcs"][0]["state"] == "ready"
    finally:
        checks.close()
    assert all(process.poll() is not None for process in processes)


def test_real_socket_capability_and_passive_request(monkeypatch):
    calls = []

    class Checks:
        def collect(self, state, *, requested):
            calls.append(requested)
            return {"result": "pending", "endpoints": []}

    with tempfile.TemporaryDirectory(prefix="sm-", dir="/tmp") as temporary:
        monkeypatch.setattr(runtime, "directory", lambda: Path(temporary) / "d")
        owner = str(uuid4())
        service = runtime.RuntimeDiagnostics(
            owner, lambda: {"session_id": owner, "vpcs": []}, {}, runtime.Events(), Checks()
        )
        try:
            service.start()
            assert "resource_checks" in runtime.snapshot(service.path)["capabilities"]
            runtime.snapshot(service.path, resource_checks=True)
            assert calls == [False, True]
            assert protocol.request() == {
                "version": 1,
                "operation": "snapshot",
                "after_sequence": 0,
            }
            for flag in ("yes", 1, [], None):
                with pytest.raises(ValueError, match="Invalid resource checks flag"):
                    protocol.validate_request(dict(protocol.request(), resource_checks=flag))
        finally:
            service.close()
