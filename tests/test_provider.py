import json

import pulumi
import pulumi_aws
from pytest import raises

from stelvio.aws import default_region
from stelvio.aws.topic import Topic
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.exceptions import StelvioValidationError
from stelvio.provider import ProviderStore
from tests.aws.pulumi_mocks import R, provider_urn

# --- Region resolution ---


def test_default_region_falls_back_to_aws_chain(no_region_context):
    """With no region configured, the region resolves via the standard AWS chain."""
    assert default_region() == "eu-central-1"


def test_default_region_prefers_aws_region_over_aws_default_region(no_region_context, monkeypatch):
    """AWS_REGION wins over AWS_DEFAULT_REGION — pins the explicit env check that works
    around boto3 reading only AWS_DEFAULT_REGION (boto/boto3#3620)."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-3")
    assert default_region() == "eu-central-1"


def test_default_region_falls_back_to_boto3_chain(no_region_context, monkeypatch):
    """AWS_DEFAULT_REGION resolves too — via boto3, which ignores AWS_REGION (boto/boto3#3620)."""
    monkeypatch.delenv("AWS_REGION")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-3")
    assert default_region() == "eu-west-3"


def test_default_region_config_override_wins(monkeypatch):
    """AwsConfig(region=...) beats whatever the AWS chain would resolve."""
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    assert default_region() == "us-east-1"  # from the default test context


def test_default_region_unresolvable_raises_friendly_error(no_region_context, monkeypatch):
    """Nothing configured anywhere: a friendly error, not a silent us-east-1 or a traceback."""
    monkeypatch.delenv("AWS_REGION")
    with raises(StelvioValidationError, match="No AWS region configured"):
        default_region()


def test_default_region_with_unknown_profile_raises_friendly_error(no_region_context, monkeypatch):
    """A profile the machine doesn't have fails while resolving the region — the first
    AWS touch of every command, before Home exists — and still gets the setup message.
    No region part: none was resolved."""
    monkeypatch.delenv("AWS_REGION")
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test", env="test", aws=AwsConfig(profile="nope"), home="aws", customize={}
        )
    )

    with raises(StelvioValidationError, match="could not be found") as exc:
        default_region()

    assert str(exc.value) == (
        "The config profile (nope) could not be found\n"
        "  Profile: 'nope' (AwsConfig in stlv_app.py)\n"
        "  Check your AWS setup: 'aws configure', 'aws sso login --profile nope', "
        "AWS_PROFILE, or AwsConfig(profile=...) in stlv_app.py."
    )


def test_aws_provider_created_with_resolved_region(pulumi_mocks, no_region_context):
    """The provider gets the chain-resolved region explicitly — never an unset region."""

    @pulumi.runtime.test
    def deploy():
        return ProviderStore.aws()

    deploy()

    pulumi_mocks.assert_res(
        "stelvio-aws", inputs={"region": "eu-central-1"}, partial=True, prefixed=False
    )


@pulumi.runtime.test
def test_aws_for_region_returns_default_when_matches_resolved_region(
    pulumi_mocks, no_region_context
):
    """With no configured region, aws_for_region(resolved) still reuses the main provider."""
    default = ProviderStore.aws()
    regional = ProviderStore.aws_for_region("eu-central-1")
    assert default is regional


# --- Region of a provider / component ---
# pulumi_mocks isn't asserted on here — Provider objects are Pulumi resources, and
# creating one in-process needs mocks registered. "us-east-1" is the region the autouse
# app_context fixture configures for every test.


@pulumi.runtime.test
def test_region_of_answers_for_store_providers(pulumi_mocks):
    """region_of answers plainly for the main and any regional provider.

    The regional branch has no component using it yet (components all deploy through
    the main provider) — it's the multi-region seam, so it's pinned directly here.
    """
    assert ProviderStore.region_of(ProviderStore.aws()) == "us-east-1"
    assert ProviderStore.region_of(ProviderStore.aws_for_region("eu-west-1")) == "eu-west-1"


@pulumi.runtime.test
def test_region_of_foreign_provider_raises(pulumi_mocks):
    """A provider the store didn't create fails loudly instead of guessing a region."""
    foreign = pulumi_aws.Provider("foreign")
    with raises(ValueError, match="not created by ProviderStore"):
        ProviderStore.region_of(foreign)


# --- Provider creation and configuration ---


def test_aws_creates_provider(pulumi_mocks):
    """ProviderStore.aws() creates exactly one AWS provider, with region, profile and
    auto-tags from AppContext."""

    @pulumi.runtime.test
    def deploy():
        return ProviderStore.aws()

    deploy()

    assert len(pulumi_mocks.created(R.AWS_PROVIDER)) == 1
    p = pulumi_mocks.assert_res("stelvio-aws", R.AWS_PROVIDER, prefixed=False)
    # defaultTags is recorded as a JSON string
    assert {**p.inputs, "defaultTags": json.loads(p.inputs["defaultTags"])} == {
        "region": "us-east-1",
        "profile": "default",
        "skipCredentialsValidation": "false",
        "skipRegionValidation": "true",
        "defaultTags": {"tags": {"stelvio:app": "test", "stelvio:env": "test"}},
    }


def test_aws_provider_merges_global_tags_with_auto_tags(pulumi_mocks):
    """Provider default tags include app-level tags plus auto-tags."""
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            tags={"Team": "platform", "CostCenter": "ops"},
            customize={},
        )
    )

    @pulumi.runtime.test
    def deploy():
        return ProviderStore.aws()

    deploy()

    p = pulumi_mocks.assert_res("stelvio-aws", R.AWS_PROVIDER, prefixed=False)
    assert json.loads(p.inputs["defaultTags"]) == {
        "tags": {
            "stelvio:app": "test",
            "stelvio:env": "test",
            "Team": "platform",
            "CostCenter": "ops",
        }
    }


def test_aws_provider_global_tags_override_auto_tags(pulumi_mocks):
    """Global tags override auto-tags on key conflicts."""
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            tags={"stelvio:env": "custom-env", "stelvio:app": "custom-app"},
            customize={},
        )
    )

    @pulumi.runtime.test
    def deploy():
        return ProviderStore.aws()

    deploy()

    p = pulumi_mocks.assert_res("stelvio-aws", R.AWS_PROVIDER, prefixed=False)
    assert json.loads(p.inputs["defaultTags"]) == {
        "tags": {"stelvio:app": "custom-app", "stelvio:env": "custom-env"}
    }


# --- Caching ---


@pulumi.runtime.test
def test_aws_caches_provider(pulumi_mocks):
    """Calling aws() twice returns the same provider instance."""
    p1 = ProviderStore.aws()
    p2 = ProviderStore.aws()
    assert p1 is p2


@pulumi.runtime.test
def test_reset_clears_providers(pulumi_mocks):
    """After reset(), aws() creates a new provider."""
    p1 = ProviderStore.aws()
    ProviderStore.reset()
    p2 = ProviderStore.aws()
    assert p1 is not p2


# --- Cross-region providers ---


def test_aws_for_region_creates_provider(pulumi_mocks):
    """aws_for_region() creates a provider with the specified region and the same
    profile and auto-tags."""

    @pulumi.runtime.test
    def deploy():
        return ProviderStore.aws_for_region("eu-west-1")

    deploy()

    assert len(pulumi_mocks.created(R.AWS_PROVIDER)) == 1
    p = pulumi_mocks.assert_res("stelvio-aws-eu-west-1", R.AWS_PROVIDER, prefixed=False)
    # defaultTags is recorded as a JSON string
    assert {**p.inputs, "defaultTags": json.loads(p.inputs["defaultTags"])} == {
        "region": "eu-west-1",
        "profile": "default",
        "skipCredentialsValidation": "false",
        "skipRegionValidation": "true",
        "defaultTags": {"tags": {"stelvio:app": "test", "stelvio:env": "test"}},
    }


@pulumi.runtime.test
def test_aws_for_region_caches_per_region(pulumi_mocks):
    """Same region called twice returns the same provider."""
    p1 = ProviderStore.aws_for_region("eu-west-1")
    p2 = ProviderStore.aws_for_region("eu-west-1")
    assert p1 is p2


@pulumi.runtime.test
def test_aws_for_region_different_regions(pulumi_mocks):
    """Different regions return different provider instances."""
    p1 = ProviderStore.aws_for_region("eu-west-1")
    p2 = ProviderStore.aws_for_region("ap-southeast-1")
    assert p1 is not p2


@pulumi.runtime.test
def test_aws_for_region_returns_default_when_same_region(pulumi_mocks):
    """aws_for_region() with the default region returns the main provider."""
    default = ProviderStore.aws()
    regional = ProviderStore.aws_for_region("us-east-1")
    assert default is regional


@pulumi.runtime.test
def test_reset_clears_regional_providers(pulumi_mocks):
    """reset() also clears regional provider cache."""
    p1 = ProviderStore.aws_for_region("eu-west-1")
    ProviderStore.reset()
    p2 = ProviderStore.aws_for_region("eu-west-1")
    assert p1 is not p2


@pulumi.runtime.test
def test_context_change_recreates_default_provider(pulumi_mocks):
    """Provider cache invalidates automatically when context changes."""
    p1 = ProviderStore.aws()

    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="prod",
            aws=AwsConfig(profile="prod-profile", region="eu-west-1"),
            home="aws",
            customize={},
        )
    )

    p2 = ProviderStore.aws()
    assert p1 is not p2


@pulumi.runtime.test
def test_context_change_recreates_default_provider_when_tags_change(pulumi_mocks):
    """Provider cache invalidates when only context tags change."""
    p1 = ProviderStore.aws()

    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            tags={"Team": "platform"},
            customize={},
        )
    )

    p2 = ProviderStore.aws()
    assert p1 is not p2


@pulumi.runtime.test
def test_context_change_recreates_regional_provider(pulumi_mocks):
    """Regional cache also invalidates on context change."""
    p1 = ProviderStore.aws_for_region("us-east-1")

    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="another-app",
            env="prod",
            aws=AwsConfig(profile="prod-profile", region="ap-southeast-1"),
            home="aws",
            customize={},
        )
    )

    p2 = ProviderStore.aws_for_region("us-east-1")
    assert p1 is not p2


# --- Provider propagation ---


def test_provider_propagates_to_child_resources(pulumi_mocks):
    """Child resources created by a Component receive the ProviderStore provider."""

    @pulumi.runtime.test
    def deploy():
        return Topic("my-topic").resources

    deploy()

    [sns_topic] = pulumi_mocks.created(R.TOPIC)
    assert sns_topic.provider == provider_urn("stelvio-aws")
