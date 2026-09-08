"""Explicit AWS Provider management for Stelvio.

ProviderStore creates and caches AWS providers with auto-tags and
consistent configuration. All Stelvio components use these providers
instead of relying on the implicit default provider.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, ClassVar

import boto3
import pulumi_aws

from stelvio.exceptions import StelvioValidationError

if TYPE_CHECKING:
    from typing import Any

    import pulumi

    from stelvio.component import Component
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
    _region: ClassVar[str | None] = None

    @classmethod
    def region(cls) -> str:
        """Get the app's default AWS region — the one the main provider uses.

        The user's `AwsConfig.region` override if set, otherwise resolved via the
        standard AWS chain (AWS_REGION, AWS_DEFAULT_REGION, profile config). Every
        provider is created from this resolved value, so the two cannot diverge.
        """
        ctx = cls._get_context()
        cls._reset_if_context_changed(ctx)
        if cls._region is None:
            cls._region = cls._resolve_region(ctx)
        return cls._region

    @staticmethod
    def _resolve_region(ctx: AppContext) -> str:
        if ctx.aws.region:
            return ctx.aws.region
        # boto3 ignores AWS_REGION (boto/boto3#3620) but the Pulumi AWS provider —
        # and our docs — honor it, so check it explicitly before the boto3 chain
        # (AWS_DEFAULT_REGION, profile config files).
        region = (
            os.environ.get("AWS_REGION") or boto3.Session(profile_name=ctx.aws.profile).region_name
        )
        if not region:
            profile_hint = f" (profile: {ctx.aws.profile!r})" if ctx.aws.profile else ""
            raise StelvioValidationError(
                f"No AWS region configured{profile_hint}. Set one via "
                "AwsConfig(region=...) in @app.config, the AWS_REGION or "
                "AWS_DEFAULT_REGION environment variable, or your AWS profile."
            )
        return region

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
        if region == cls.region():
            if cls._aws is None:
                cls._aws = cls._create_aws_provider("stelvio-aws", ctx)
            return cls._aws
        if region not in cls._regional_aws:
            cls._regional_aws[region] = cls._create_aws_provider(
                f"stelvio-aws-{region}", ctx, region_override=region
            )
        return cls._regional_aws[region]

    @classmethod
    def region_of(cls, provider: pulumi.ProviderResource) -> str:
        """Plain-str region of a provider created by this store.

        Reading `region` off the provider object would give an Output; the store
        created every provider from a known region, so it can answer directly.
        """
        if provider is cls._aws:
            return cls.region()
        for region, regional in cls._regional_aws.items():
            if regional is provider:
                return region
        raise ValueError("Provider was not created by ProviderStore")

    @classmethod
    def reset(cls) -> None:
        """Clear all providers. Used for testing."""
        cls._aws = None
        cls._regional_aws = {}
        cls._context_key = None
        cls._region = None

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
            region=region_override or cls.region(),
            profile=ctx.aws.profile,
            default_tags=pulumi_aws.ProviderDefaultTagsArgs(tags=all_tags),
        )


def aws_region_of(component: Component[Any, Any]) -> str:
    """Plain-str region the component's AWS provider deploys to."""
    return ProviderStore.region_of(component._provider)  # noqa: SLF001
