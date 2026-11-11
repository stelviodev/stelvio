"""Laptop session identity passed to the isolated networking runtime."""

from dataclasses import dataclass
from typing import final
from uuid import UUID

from stelvio.network import NetworkManifest

SESSION_VERSION = 1


@final
@dataclass(frozen=True)
class SessionDescription:
    session_id: str
    app: str
    environment: str
    owner_uid: int
    manifest: NetworkManifest
    version: int = SESSION_VERSION

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != SESSION_VERSION:
            raise ValueError("Unsupported tunnel session version")
        if not isinstance(self.session_id, str) or str(UUID(self.session_id)) != self.session_id:
            raise ValueError("Tunnel session ID must be a canonical UUID")
        if type(self.owner_uid) is not int or self.owner_uid <= 0:
            raise ValueError("Tunnel session owner must be a nonroot UID")
        for field in ("app", "environment"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or not value
                or any(char.isspace() or not char.isprintable() for char in value)
            ):
                raise ValueError(f"Tunnel session {field!r} must be a nonempty string")
