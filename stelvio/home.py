from pathlib import Path
from typing import Protocol


class Home(Protocol):
    """Storage interface - params and files. Dumb I/O, no domain logic."""

    # Params (SSM in AWS, KV in Cloudflare, etc.)
    def read_param(self, name: str) -> str | None: ...
    def write_param(
        self, name: str, value: str, description: str = "", *, secure: bool = False
    ) -> None: ...

    # Storage init (S3 bucket in AWS, directory locally, etc.)
    def init_storage(self, name: str | None = None) -> str:
        """
        Initialize storage for file operations.

        If name is None: generate provider-specific name, create storage, return name.
        If name provided: use that storage (assumes exists), return name.
        """
        ...

    # Files (S3 in AWS, filesystem locally, etc.)
    def read_file(self, key: str, local_path: Path) -> bool:
        """Download file to local_path. Returns True if file existed."""
        ...

    def write_file(self, key: str, local_path: Path) -> None:
        """Upload file from local_path."""
        ...

    def delete_file(self, key: str) -> None:
        """Delete file."""
        ...

    def file_exists(self, key: str) -> bool:
        """Check if file exists."""
        ...

    def delete_prefix(self, prefix: str) -> None:
        """Delete all files with given prefix."""
        ...
