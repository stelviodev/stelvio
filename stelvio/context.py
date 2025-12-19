from dataclasses import dataclass
from typing import ClassVar, Literal

from stelvio.config import AwsConfig
from stelvio.dns import Dns


@dataclass(frozen=True)
class AppContext:
    """Context information available during Stelvio app execution."""

    name: str
    env: str
    aws: AwsConfig
    home: Literal["aws"]
    dns: Dns | None = None
    dev_mode: bool = False

    def prefix(self, name: str | None = None) -> str:
        """Get resource name prefix or prefixed name.

        Args:
            name: Optional name to prefix. If None, returns just the prefix with trailing dash.

        Returns:
            If name is None: "{app}-{env}-"
            If name provided: "{app}-{env}-{name}"
        """
        base = f"{self.name.lower()}-{self.env.lower()}-"
        return base if name is None else f"{base}{name}"


class _ContextStore:
    """Internal storage for the global app context."""

    _instance: ClassVar[AppContext | None] = None

    @classmethod
    def set(cls, context: AppContext) -> None:
        """Set the global context. Can only be called once."""
        if cls._instance is not None:
            raise RuntimeError("Context has already been initialized")
        cls._instance = context

    @classmethod
    def get(cls) -> AppContext:
        """Get the global context."""
        if cls._instance is None:
            raise RuntimeError(
                "Stelvio context not initialized. This usually means you're trying to access "
                "context() outside of a Stelvio deployment operation."
            )
        return cls._instance

    @classmethod
    def clear(cls) -> None:
        """Clear the context. Only used for testing."""
        cls._instance = None


def context() -> AppContext:
    """Get the current Stelvio app context.

    Returns:
        AppContext with app name, environment, and AWS configuration.

    Raises:
        RuntimeError: If called before context is initialized.
    """
    return _ContextStore.get()
