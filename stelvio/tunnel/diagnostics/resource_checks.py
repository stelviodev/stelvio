"""Demand-driven TLS observations, isolated from networking and readiness workers."""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from ipaddress import IPv4Address, IPv4Network
from threading import Event, Lock, Thread

from stelvio.tunnel.diagnostics.protocol import timestamp

MAX_ENDPOINTS = 256
MAX_CA_BYTES = 128 * 1024
ROUND_SECONDS = 15
INTERVAL_SECONDS = 30
STALE_SECONDS = 60


def targets(state: dict) -> list[dict]:
    """Use only ready, discovered resources; retain the fencing identity."""
    result = []
    for vpc in state.get("vpcs", []):
        if vpc.get("state") != "ready":
            continue
        for resource in vpc.get("resources", []):
            if resource.get("service") != "documentdb":
                continue
            for host in resource.get("hostnames", []):
                result.extend(
                    {
                        "unit": vpc["unit"],
                        "generation": vpc["generation"],
                        "attempt": vpc["attempt"],
                        "dns_revision": vpc["dns_revision"],
                        "resource": resource["identity"],
                        "host": host,
                        "port": port,
                        "ranges": vpc["ranges"],
                    }
                    for port in resource.get("ports", [])
                )
    return sorted(
        result, key=lambda item: (item["unit"], item["resource"], item["host"], item["port"])
    )


def check_endpoint(target: dict, context: ssl.SSLContext) -> dict:
    started = time.monotonic()
    result = {key: value for key, value in target.items() if key != "ranges"}
    try:
        networks = [IPv4Network(value) for value in target["ranges"]]
        addresses = socket.getaddrinfo(
            target["host"], target["port"], socket.AF_INET, socket.SOCK_STREAM
        )
        if not addresses or any(
            not any(IPv4Address(address[4][0]) in network for network in networks)
            for address in addresses
        ):
            result.update(result="failed", error="outside-routed-subnets")
            return result
        deadline = started + 2
        # Authenticate the real endpoint name, even though connect uses its numeric address.
        for family, kind, protocol, _, address in addresses:
            with socket.socket(family, kind, protocol) as connection:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return dict(result, result="failed", error="timeout")
                connection.settimeout(remaining)
                connection.connect(address)
                connection.settimeout(max(0.001, deadline - time.monotonic()))
                with context.wrap_socket(connection, server_hostname=target["host"]) as secured:
                    secured.do_handshake()
        result.update(result="passed", error=None)
    except ssl.SSLCertVerificationError:
        result.update(result="failed", error="certificate-verification")
    except TimeoutError:
        result.update(result="failed", error="timeout")
    except (OSError, ValueError):
        result.update(result="failed", error="endpoint-unavailable")
    result["duration_seconds"] = time.monotonic() - started
    return result


def run_checks(payload: dict) -> list[dict]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=payload["ca_pem"])
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(lambda target: check_endpoint(target, context), payload["targets"]))


class ResourceChecks:
    """Snapshot handlers schedule work but never wait for endpoint I/O."""

    def __init__(self, ca_pem: str | None) -> None:
        self.ca_pem = ca_pem
        self.lock = Lock()
        self.stop = Event()
        self.thread = None
        self.key = None
        self.value = None
        self.observed = None
        self.last_started = float("-inf")

    def collect(self, state: dict, *, requested: bool) -> dict:
        current = targets(state)
        key = json.dumps(current, sort_keys=True)
        with self.lock:
            if key != self.key:
                self.key, self.value, self.observed = key, None, None
            now = time.monotonic()
            running = self.thread is not None and self.thread.is_alive()
            if (
                requested
                and current
                and not self.stop.is_set()
                and not running
                and now - self.last_started >= INTERVAL_SECONDS
            ):
                self.last_started = now
                self.thread = Thread(target=self._run, args=(key, current), daemon=True)
                self.thread.start()
            result = (
                deepcopy(self.value)
                if self.value
                else {
                    "result": "unavailable" if not self.ca_pem else "pending",
                    "error": "missing-ca-bundle" if not self.ca_pem else None,
                    "checked": 0,
                    "total": len(current),
                    "endpoints": [],
                    "observed_at": None,
                }
            )
            if not current:
                result.update(result="not-applicable", error=None)
            result["age_seconds"] = now - self.observed if self.observed is not None else None
            result["stale"] = self.observed is None or result["age_seconds"] > STALE_SECONDS
            result["running"] = running
            return result

    def _run(self, key: str, current: list[dict]) -> None:
        started = time.monotonic()
        endpoints = []
        error = None
        if not self.ca_pem:
            error = "missing-ca-bundle"
        elif len(current) > MAX_ENDPOINTS:
            error = "endpoint-limit"
        else:
            try:
                endpoints = self._exchange(key, current, started)
            except TimeoutError:
                error = "round-timeout"
            except (OSError, ValueError):
                error = "invalid-ca-or-worker-failure"
        value = {
            "result": "unavailable"
            if error
            else (
                "passed"
                if endpoints and all(item["result"] == "passed" for item in endpoints)
                else "failed"
            ),
            "error": error,
            "checked": len(endpoints),
            "total": len(current),
            "endpoints": endpoints,
            "observed_at": timestamp(),
            "duration_seconds": time.monotonic() - started,
        }
        with self.lock:
            if key == self.key and not self.stop.is_set():
                self.value, self.observed = value, time.monotonic()

    def _exchange(self, key: str, current: list[dict], started: float) -> list[dict]:
        payload = json.dumps({"ca_pem": self.ca_pem, "targets": current}).encode()
        if len(payload) > 512 * 1024:
            raise ValueError("TLS input limit")
        with subprocess.Popen(  # noqa: S603 - fixed isolated worker; no credentials in input
            [sys.executable, "-I", "-m", __name__],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            try:
                while not self.stop.is_set() and time.monotonic() - started < ROUND_SECONDS:
                    with self.lock:
                        if self.key != key:
                            raise TimeoutError
                    try:
                        output, _ = process.communicate(payload, timeout=0.1)
                        if process.returncode or len(output) > 256 * 1024:
                            raise ValueError("TLS worker failed")
                        return json.loads(output)
                    except subprocess.TimeoutExpired:
                        payload = None
                raise TimeoutError
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def close(self) -> None:
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)


if __name__ == "__main__":
    sys.stdout.write(json.dumps(run_checks(json.loads(sys.stdin.buffer.read(512 * 1024)))))
