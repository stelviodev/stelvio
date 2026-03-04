import json

import pulumi

from stelvio.aws.topic import Topic
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.provider import ProviderStore

# --- Provider creation and configuration ---


@pulumi.runtime.test
def test_aws_creates_provider(pulumi_mocks):
    """ProviderStore.aws() creates exactly one AWS provider."""
    provider = ProviderStore.aws()

    def check(_):
        providers = pulumi_mocks.created_providers()
        assert len(providers) == 1
        assert providers[0].typ == "pulumi:providers:aws"
        assert providers[0].name == "stelvio-aws"

    provider.region.apply(check)


@pulumi.runtime.test
def test_aws_provider_region_and_profile(pulumi_mocks):
    """Provider uses region and profile from AppContext."""
    provider = ProviderStore.aws()

    def check(_):
        p = pulumi_mocks.created_providers()[0]
        assert p.typ == "pulumi:providers:aws"
        assert p.inputs["region"] == "us-east-1"
        assert p.inputs["profile"] == "default"

    provider.region.apply(check)


@pulumi.runtime.test
def test_aws_provider_auto_tags(pulumi_mocks):
    """Provider has stelvio:app and stelvio:env auto-tags."""
    provider = ProviderStore.aws()

    def check(_):
        p = pulumi_mocks.created_providers()[0]
        assert p.typ == "pulumi:providers:aws"
        tags = json.loads(p.inputs["defaultTags"])["tags"]
        assert tags["stelvio:app"] == "test"
        assert tags["stelvio:env"] == "test"

    provider.region.apply(check)


@pulumi.runtime.test
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

    provider = ProviderStore.aws()

    def check(_):
        p = pulumi_mocks.created_providers()[0]
        tags = json.loads(p.inputs["defaultTags"])["tags"]
        assert tags["stelvio:app"] == "test"
        assert tags["stelvio:env"] == "test"
        assert tags["Team"] == "platform"
        assert tags["CostCenter"] == "ops"

    provider.region.apply(check)


@pulumi.runtime.test
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

    provider = ProviderStore.aws()

    def check(_):
        p = pulumi_mocks.created_providers()[0]
        tags = json.loads(p.inputs["defaultTags"])["tags"]
        assert tags["stelvio:app"] == "custom-app"
        assert tags["stelvio:env"] == "custom-env"

    provider.region.apply(check)


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


@pulumi.runtime.test
def test_aws_for_region_creates_provider(pulumi_mocks):
    """aws_for_region() creates a provider with the specified region."""
    provider = ProviderStore.aws_for_region("eu-west-1")

    def check(_):
        providers = pulumi_mocks.created_providers("stelvio-aws-eu-west-1")
        assert len(providers) == 1
        p = providers[0]
        assert p.typ == "pulumi:providers:aws"
        assert p.inputs["region"] == "eu-west-1"
        assert p.inputs["profile"] == "default"

    provider.region.apply(check)


@pulumi.runtime.test
def test_aws_for_region_auto_tags(pulumi_mocks):
    """Cross-region provider has the same auto-tags."""
    provider = ProviderStore.aws_for_region("eu-west-1")

    def check(_):
        p = pulumi_mocks.created_providers("stelvio-aws-eu-west-1")[0]
        assert p.typ == "pulumi:providers:aws"
        tags = json.loads(p.inputs["defaultTags"])["tags"]
        assert tags["stelvio:app"] == "test"
        assert tags["stelvio:env"] == "test"

    provider.region.apply(check)


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


@pulumi.runtime.test
def test_provider_propagates_to_child_resources(pulumi_mocks):
    """Child resources created by a Component receive the ProviderStore provider."""
    topic = Topic("my-topic")
    resources = topic.resources

    def check(_):
        sns_topics = [r for r in pulumi_mocks.created_resources if r.typ == "aws:sns/topic:Topic"]
        assert len(sns_topics) == 1
        assert sns_topics[0].provider is not None
        provider_name = sns_topics[0].provider.rsplit("::", 1)[-1]
        normalized_provider_name = provider_name.removesuffix("-test-id")
        assert normalized_provider_name == "stelvio-aws", (
            f"Expected default provider 'stelvio-aws', got '{provider_name}'"
        )

    resources.topic.arn.apply(check)
