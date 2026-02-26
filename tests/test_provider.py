import json

import pulumi
import pytest
from pulumi.runtime import set_mocks

from stelvio.aws.topic import Topic
from stelvio.provider import ProviderStore
from tests.aws.pulumi_mocks import PulumiTestMocks


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


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

    resources.topic.arn.apply(check)
