"""Explicit AWS Provider management for Stelvio.

ProviderStore creates and caches AWS providers with auto-tags and
consistent configuration. All Stelvio components use these providers
instead of relying on the implicit default provider.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import pulumi_aws

if TYPE_CHECKING:
    from stelvio.context import AppContext


type _ContextKey = tuple[str, str, str | None, str | None, tuple[tuple[str, str], ...]]


class ProviderStore:
    """Manages explicit AWS providers for Stelvio resources.

    Provides a main provider (matching the user's configured region/profile)
    and cached cross-region providers (e.g. us-east-1 for ACM certificates).
    All providers share the same auto-tags and credential configuration.

    Lazy: providers are created on first access from the current app context.
    """

    _aws: ClassVar[pulumi_aws.Provider | None] = None
    _regional_aws: ClassVar[dict[str, pulumi_aws.Provider]] = {}
    _context_key: ClassVar[_ContextKey | None] = None

    @classmethod
    def aws(cls) -> pulumi_aws.Provider:
        """Get the main AWS provider, creating it on first access."""
        ctx = cls._get_context()
        cls._reset_if_context_changed(ctx)
        if cls._aws is None:
            cls._aws = cls._create_aws_provider("stelvio-aws", ctx)
        return cls._aws

    @classmethod
    def aws_for_region(cls, region: str) -> pulumi_aws.Provider:
        """Get a cached provider for a specific AWS region.

        Used by components that need cross-region resources (e.g. ACM
        certificates in us-east-1 for CloudFront distributions).
        Returns the main provider if the region matches the default.
        """
        ctx = cls._get_context()
        cls._reset_if_context_changed(ctx)
        if region == ctx.aws.region:
            if cls._aws is None:
                cls._aws = cls._create_aws_provider("stelvio-aws", ctx)
            return cls._aws
        if region not in cls._regional_aws:
            cls._regional_aws[region] = cls._create_aws_provider(
                f"stelvio-aws-{region}", ctx, region_override=region
            )
        return cls._regional_aws[region]

    @classmethod
    def reset(cls) -> None:
        """Clear all providers. Used for testing."""
        cls._aws = None
        cls._regional_aws = {}
        cls._context_key = None

    @classmethod
    def _reset_if_context_changed(cls, ctx: AppContext) -> None:
        """Reset provider cache when app context changes within one process.

        Stelvio's CLI is typically one-shot, but tests/dev flows can run multiple
        contexts in a single Python process. Provider resources capture region,
        profile, and default tags at creation time, so stale cached providers must
        never leak across context boundaries.
        """
        new_key = cls._context_cache_key(ctx)
        if cls._context_key is None:
            cls._context_key = new_key
            return
        if cls._context_key != new_key:
            cls.reset()
            cls._context_key = new_key

    @staticmethod
    def _context_cache_key(ctx: AppContext) -> _ContextKey:
        return (
            ctx.name,
            ctx.env,
            ctx.aws.region,
            ctx.aws.profile,
            tuple(sorted(ctx.tags.items())),
        )

    @classmethod
    def _get_context(cls) -> AppContext:
        from stelvio.context import _ContextStore  # noqa: PLC0415

        return _ContextStore.get()

    @classmethod
    def _create_aws_provider(
        cls,
        name: str,
        ctx: AppContext,
        region_override: str | None = None,
    ) -> pulumi_aws.Provider:
        all_tags = {
            "stelvio:app": ctx.name,
            "stelvio:env": ctx.env,
            **ctx.tags,
        }
        return pulumi_aws.Provider(
            name,
            region=region_override or ctx.aws.region,
            profile=ctx.aws.profile,
            default_tags=pulumi_aws.ProviderDefaultTagsArgs(tags=all_tags),
        )
