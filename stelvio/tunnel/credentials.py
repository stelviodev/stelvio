"""Capture provider startup context for the separate nonroot network runtime.

The environment is transient process input, never network metadata or a recovery
record. SDK sessions are opened only in the child, retaining the normal provider
chain and its refresh callbacks independently of handler environment scopes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import boto3
from botocore.config import Config

from stelvio.tunnel.access_program import CapturedCredentials
from stelvio.tunnel.manifest import ACCOUNT

if TYPE_CHECKING:
    from collections.abc import Mapping

AWS_IO = Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2})
_FILE_VARIABLES = (
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
    "AWS_CA_BUNDLE",
)


@dataclass(frozen=True)
class AwsExecutionContext:
    provider: str
    account: str
    region: str
    profile: str | None
    owner_uid: int
    parent_pid: int
    _environment: Mapping[str, str] = field(repr=False)
    _session: boto3.Session | None = field(default=None, init=False, repr=False, compare=False)

    @classmethod
    def capture(
        cls, *, provider: str, account: str, region: str, profile: str | None = None
    ) -> AwsExecutionContext:
        if not provider or not ACCOUNT.fullmatch(account) or not region:
            raise ValueError("AWS runtime requires resolved provider/account/region identity")
        if not os.geteuid() or os.getuid() != os.geteuid():
            raise RuntimeError("Capture AWS networking context as the ordinary nonroot user")
        environment = dict(os.environ)
        for key in _FILE_VARIABLES:
            if environment.get(key):
                # Resolve before the caller's working directory/environment changes.
                environment[key] = str(Path(environment[key]).expanduser().absolute())
        return cls(
            provider,
            account,
            region,
            profile,
            os.geteuid(),
            os.getpid(),
            MappingProxyType(environment),
        )

    def process_environment(self) -> dict[str, str]:
        """A fresh child environment; never merge the handler's current variables."""
        return dict(self._environment)

    def _require_runtime(self) -> None:
        if os.getpid() == self.parent_pid or os.geteuid() != self.owner_uid or not self.owner_uid:
            raise RuntimeError("AWS networking sessions belong to the separate nonroot runtime")
        # Detect accidental launch with mutable handler authentication inputs.
        expected = {
            key: value
            for key, value in self._environment.items()
            if key.startswith("AWS_") or key in ("HOME", "PATH")
        }
        actual = {
            key: value
            for key, value in os.environ.items()
            if key.startswith("AWS_") or key in ("HOME", "PATH")
        }
        if actual != expected:
            raise RuntimeError("AWS runtime was launched with a different credential context")

    def open_session(self) -> boto3.Session:
        self._require_runtime()
        if self._session is not None:
            return self._session
        session = boto3.Session(profile_name=self.profile, region_name=self.region)
        credentials = session.get_credentials()
        if credentials is None:
            raise RuntimeError("Captured AWS credential source is unavailable")
        if session.client("sts", config=AWS_IO).get_caller_identity()["Account"] != self.account:
            raise RuntimeError("Captured AWS credential source belongs to another account")
        object.__setattr__(self, "_session", session)
        return session

    def frozen_credentials(self, session: boto3.Session) -> CapturedCredentials:
        """Refresh through the original SDK provider before a new engine/transport."""
        self._require_runtime()
        if session is not self._session:
            raise RuntimeError("Credential refresh received another provider's SDK session")
        if session.region_name != self.region:
            raise RuntimeError("Captured AWS provider region changed")
        # get_frozen_credentials preserves SDK refresh and propagates expiration
        # of fixed startup credentials. No fallback to the handler environment.
        captured = CapturedCredentials.capture(session)
        if captured.account != self.account:
            raise RuntimeError("Refreshed AWS credential source belongs to another account")
        return captured
