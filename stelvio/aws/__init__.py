"""AWS components for Stelvio."""

from stelvio.provider import ProviderStore

__all__ = ["default_region"]


def default_region() -> str:
    """Get the app's default AWS region.

    The region set in `AwsConfig(region=...)`, or — when not set — the one resolved
    from the standard AWS chain (`AWS_REGION`, `AWS_DEFAULT_REGION`, profile config).
    This is the region all Stelvio resources deploy to unless a component says otherwise.

    Raises:
        StelvioValidationError: If no region is configured anywhere.
        RuntimeError: If called outside a Stelvio deployment operation.
    """
    return ProviderStore.region()
