"""CLI-owned network lifetime and bounded, snapshot-based invocation admission."""

from __future__ import annotations

import os
import shlex
import time
from threading import Event, Lock, Thread
from typing import TYPE_CHECKING
from uuid import uuid4

from stelvio.exceptions import StelvioValidationError
from stelvio.stack_outputs import read_network_manifest
from stelvio.tunnel.actors import AWS_VERSION
from stelvio.tunnel.credentials import AwsExecutionContext
from stelvio.tunnel.installation import install_helper
from stelvio.tunnel.manifest import SessionDescription
from stelvio.tunnel.policy import BastionPolicy
from stelvio.tunnel.runtime import NetworkRuntime

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


STATUS_MAX_AGE = 5


class VpcUnavailableError(RuntimeError):
    """The endpoint's current network dependencies cannot admit an invocation."""


class DevNetworkSession:
    def __init__(  # noqa: PLR0913 - captured deployment/home inputs
        self,
        *,
        state: dict | None,
        app: str,
        env: str,
        home: dict,
        config_dir: Path,
        report: Callable[[str], None],
    ) -> None:
        try:
            self.manifest = read_network_manifest(state)
        except ValueError as error:
            raise StelvioValidationError(str(error)) from error
        self.description = None
        self.contexts = ()
        self.settings = None
        self.report = report
        self.runtime = None
        self._lock = Lock()
        self._stop = Event()
        self._thread = None
        self._statuses = {}
        self._observed = 0.0
        self._closed = False
        disabled = {
            v.identity for v in self.manifest.used_vpcs if v.policy == BastionPolicy.DISABLED
        }
        self.dependencies = {
            e.endpoint_id: tuple(v for v in e.vpcs if v not in disabled)
            for e in self.manifest.endpoints
        }
        for identity in sorted(disabled):
            report(f"VPC access disabled: {identity}; local networking is your responsibility.")
        if not self.manifest.enabled_vpcs:
            return
        self.description = SessionDescription(str(uuid4()), app, env, os.geteuid(), self.manifest)
        providers = {
            r["urn"]: r for r in _resources(state) if r.get("type") == "pulumi:providers:aws"
        }
        contexts = {}
        for vpc in self.manifest.enabled_vpcs:
            provider = providers.get(vpc.provider)
            if provider is None:
                raise StelvioValidationError("Missing resolved AWS provider; redeploy the app.")
            inputs = provider.get("inputs") or {}
            # SDK profile/normal startup chain is the supported provider contract.
            if any(
                inputs.get(k)
                for k in (
                    "accessKey",
                    "secretKey",
                    "token",
                    "assumeRole",
                    "assumeRoles",
                    "assumeRoleWithWebIdentity",
                    "sharedConfigFiles",
                    "sharedCredentialsFiles",
                    "endpoints",
                    "httpProxy",
                    "httpsProxy",
                    "noProxy",
                    "customCaBundle",
                    "insecure",
                    "ec2MetadataServiceEndpoint",
                    "ec2MetadataServiceEndpointMode",
                    "stsRegion",
                    "useDualstackEndpoint",
                    "useFipsEndpoint",
                )
            ):
                raise StelvioValidationError(
                    "Use the AWS SDK startup credential chain or an AWS profile "
                    "for VPC dev access."
                )
            profile = inputs.get("profile") or None
            if profile is not None and not isinstance(profile, str):
                raise StelvioValidationError("AWS provider profile did not resolve to a name.")
            captured = AwsExecutionContext.capture(
                provider=vpc.provider, account=vpc.account, region=vpc.region, profile=profile
            )
            previous = contexts.setdefault(vpc.provider, captured)
            if (previous.account, previous.region, previous.profile) != (
                captured.account,
                captured.region,
                captured.profile,
            ):
                raise StelvioValidationError(
                    "Conflicting AWS provider identities in network metadata."
                )
        home_provider = "stelvio-home"
        contexts[home_provider] = AwsExecutionContext.capture(
            provider=home_provider,
            account=home["account"],
            region=home["region"],
            profile=home["profile"],
        )
        self.contexts = tuple(contexts.values())
        self.settings = {
            "bucket": home["bucket"],
            "home_provider": home_provider,
            "cli": str(config_dir / "bin/pulumi"),
            "aws_provider": str(
                config_dir
                / ".pulumi/plugins"
                / f"resource-aws-v{AWS_VERSION}"
                / "pulumi-resource-aws"
            ),
        }

    def start(self) -> None:
        if self._closed or self.runtime is not None:
            raise RuntimeError("Network session is already started or closed")
        if self.description is None:
            self.report("Network READY (no enabled VPC dependencies).")
            return
        try:
            install_helper()
            self.runtime = NetworkRuntime(
                self.description, self.contexts, access_settings=self.settings
            )
        except Exception:
            self._report_recovery()
            raise
        self.report(f"Network STARTING; session {self.description.session_id}")
        self._thread = Thread(target=self._monitor, name="stelvio-network-status", daemon=True)
        self._thread.start()

    def _monitor(self) -> None:
        previous = None
        while not self._stop.is_set():
            try:
                values = self.runtime.status()
                statuses = {s["identity"]: s for s in values}
                expected = {v.identity for v in self.manifest.enabled_vpcs}
                if (
                    set(statuses) != expected
                    or len(statuses) != len(values)
                    or any(
                        s.get("state")
                        not in {"starting", "retrying", "ready", "failed", "stopped"}
                        or type(s.get("attempt")) is not int
                        or s["attempt"] < 0
                        or (s["state"] == "ready" and s["attempt"] == 0)
                        for s in values
                    )
                ):
                    raise ValueError("Incomplete network status")  # noqa: TRY301 - refuse incomplete IPC snapshot
                with self._lock:
                    self._statuses = statuses
                    self._observed = time.monotonic()
                signature = tuple(
                    (
                        s["identity"],
                        s["state"],
                        s.get("attempt"),
                        s.get("cause"),
                        s.get("dns_retained"),
                    )
                    for s in values
                )
                if signature != previous:
                    overall = "READY" if all(s["state"] == "ready" for s in values) else "DEGRADED"
                    self.report("Network " + overall)
                    for s in values:
                        self.report(
                            f"  {s['identity']}: {s['state']} "
                            f"attempt={s.get('attempt', 0)} cause={s.get('cause') or '-'} "
                            f"retained_dns={s.get('dns_retained', False)}"
                        )
                    previous = signature
            except Exception:
                with self._lock:
                    self._statuses = {}
                    self._observed = 0.0
                if previous != "unavailable":
                    self.report("Network DEGRADED: runtime status unavailable.")
                    previous = "unavailable"
            self._stop.wait(1)

    def admit(self, endpoint: str) -> VpcUnavailableError | None:
        required = self.dependencies.get(endpoint, ())
        if not required:
            return None
        with self._lock:
            stale = time.monotonic() - self._observed > STATUS_MAX_AGE
            failed = [
                v
                for v in required
                if self._closed or stale or self._statuses.get(v, {}).get("state") != "ready"
            ]
        if failed:
            return VpcUnavailableError(
                "VPC access unavailable for this invocation: " + ", ".join(failed)
            )
        return None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=12)
        if self.runtime:
            try:
                self.runtime.close()
            except Exception as error:
                self.report(f"Network cleanup incomplete: {error}")
                self._report_recovery()
                raise RuntimeError(
                    "VPC dev cleanup incomplete; preserve recovery ownership"
                ) from error

    def _report_recovery(self) -> None:
        self.report(f"Network recovery ownership: session {self.description.session_id}")
        self.report("Host recovery: stlv tunnel reconcile")
        for vpc in self.manifest.enabled_vpcs:
            self.report(f"  Inspect residual VPC access: {vpc.identity} ({vpc.vpc_id})")
        home = next(c for c in self.contexts if c.provider == self.settings["home_provider"])
        for target in self.contexts:
            if target is home:
                continue
            command = [
                "stlv",
                "tunnel",
                "recover",
                "--bucket",
                self.settings["bucket"],
                "--home-account",
                home.account,
                "--home-region",
                home.region,
                "--account",
                target.account,
                "--region",
                target.region,
                "--app",
                self.description.app,
                "--env",
                self.description.environment,
                "--session",
                self.description.session_id,
            ]
            if home.profile:
                command.extend(["--home-profile", home.profile])
            if target.profile:
                command.extend(["--profile", target.profile])
            self.report("AWS recovery: " + shlex.join(command))
        self.report("Run recovery as the original user with the original AWS credential sources.")


def _resources(state: dict | None) -> list[dict]:
    if not state:
        return []
    checkpoint = state.get("checkpoint", state)
    return checkpoint.get("latest", checkpoint).get("resources", [])
