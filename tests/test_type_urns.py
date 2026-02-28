"""Verify that all Stelvio component type URNs are correct and complete.

Maintains a canonical mapping of every Component subclass to its expected
Pulumi type URN. Guards against typos, inconsistent naming, and new
components being added without updating this list.
"""

import importlib
import pkgutil
import re

import pulumi
import pytest
from pulumi.runtime import set_mocks

import stelvio.aws
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway.api import Api
from stelvio.aws.cloudfront.cloudfront import CloudFrontDistribution
from stelvio.aws.cloudfront.origins.components.url import Url
from stelvio.aws.cloudfront.router import Router
from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoSubscription, DynamoTable
from stelvio.aws.email import Email
from stelvio.aws.function.function import Function
from stelvio.aws.layer import Layer
from stelvio.aws.queue import Queue, QueueSubscription
from stelvio.aws.s3.s3 import Bucket, BucketNotifySubscription
from stelvio.aws.s3.s3_static_website import S3StaticWebsite
from stelvio.aws.topic import Topic, TopicQueueSubscription, TopicSubscription
from stelvio.component import Component
from tests.aws.pulumi_mocks import PulumiTestMocks

# Canonical mapping: every Component subclass → its expected type URN.
# If you add a new component, add it here too.
CANONICAL_URNS: dict[type[Component], str] = {
    Function: "stelvio:aws:Function",
    Api: "stelvio:aws:Api",
    DynamoTable: "stelvio:aws:DynamoTable",
    DynamoSubscription: "stelvio:aws:DynamoSubscription",
    Bucket: "stelvio:aws:Bucket",
    BucketNotifySubscription: "stelvio:aws:BucketNotifySubscription",
    S3StaticWebsite: "stelvio:aws:S3StaticWebsite",
    Queue: "stelvio:aws:Queue",
    QueueSubscription: "stelvio:aws:QueueSubscription",
    Topic: "stelvio:aws:Topic",
    TopicSubscription: "stelvio:aws:TopicSubscription",
    TopicQueueSubscription: "stelvio:aws:TopicQueueSubscription",
    Layer: "stelvio:aws:Layer",
    Email: "stelvio:aws:Email",
    Cron: "stelvio:aws:Cron",
    CloudFrontDistribution: "stelvio:aws:CloudFrontDistribution",
    Router: "stelvio:aws:Router",
    AcmValidatedDomain: "stelvio:aws:AcmValidatedDomain",
    Url: "stelvio:aws:Url",
}


def _collect_all_component_subclasses() -> set[type]:
    """Recursively collect all Component subclasses from stelvio package."""

    def _collect(cls: type) -> set[type]:
        result = set()
        for sub in cls.__subclasses__():
            if sub.__module__.startswith("stelvio."):
                result.add(sub)
            result.update(_collect(sub))
        return result

    return _collect(Component)


def _import_all_stelvio_aws_modules() -> None:
    """Import every module under stelvio.aws to register all subclasses."""
    for _importer, modname, _ispkg in pkgutil.walk_packages(
        stelvio.aws.__path__, prefix="stelvio.aws."
    ):
        importlib.import_module(modname)


# Import all modules once at module load so __subclasses__() is complete.
_import_all_stelvio_aws_modules()


# =========================================================================
# Static verification
# =========================================================================


@pytest.mark.parametrize(
    "cls",
    CANONICAL_URNS.keys(),
    ids=[c.__name__ for c in CANONICAL_URNS],
)
def test_class_is_component_subclass(cls):
    """Every class in the canonical list is a Component subclass."""
    assert issubclass(cls, Component)


@pytest.mark.parametrize(
    ("cls", "urn"),
    CANONICAL_URNS.items(),
    ids=[c.__name__ for c in CANONICAL_URNS],
)
def test_urn_matches_pattern(cls, urn):
    """Every type URN follows the stelvio:aws:PascalCase pattern."""
    assert re.match(r"^stelvio:aws:[A-Z][a-zA-Z0-9]+$", urn), (
        f"{cls.__name__} has non-conforming URN: {urn}"
    )


def test_canonical_list_has_19_entries():
    """Exactly 19 component types exist."""
    assert len(CANONICAL_URNS) == 19


def test_canonical_list_is_complete():
    """Every Component subclass in stelvio.aws is in the canonical list.

    Catches new components being added without updating this test file.
    """
    discovered = _collect_all_component_subclasses()
    canonical_classes = set(CANONICAL_URNS.keys())
    missing = discovered - canonical_classes
    assert not missing, (
        f"Component subclasses not in CANONICAL_URNS: {sorted(c.__name__ for c in missing)}. "
        "Add them to CANONICAL_URNS in tests/test_type_urns.py."
    )


def test_no_duplicate_urns():
    """All type URN strings are unique across components."""
    urns = list(CANONICAL_URNS.values())
    assert len(urns) == len(set(urns)), (
        f"Duplicate URNs found: {[u for u in urns if urns.count(u) > 1]}"
    )


# =========================================================================
# Runtime verification (simple components only)
# =========================================================================


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    return mocks


SIMPLE_COMPONENTS = [
    ("Api", lambda: Api("test-api"), "stelvio:aws:Api"),
    ("Bucket", lambda: Bucket("test-bucket"), "stelvio:aws:Bucket"),
    (
        "Cron",
        lambda: Cron("test-cron", "rate(1 hour)", "functions/simple.handler"),
        "stelvio:aws:Cron",
    ),
    ("Queue", lambda: Queue("test-queue"), "stelvio:aws:Queue"),
    ("Topic", lambda: Topic("test-topic"), "stelvio:aws:Topic"),
    (
        "DynamoTable",
        lambda: DynamoTable("test-dynamo", partition_key="pk", fields={"pk": "S"}),
        "stelvio:aws:DynamoTable",
    ),
    ("Email", lambda: Email("test-email", "sender@example.com"), "stelvio:aws:Email"),
    (
        "Function",
        lambda: Function("test-function", handler="functions/simple.handler"),
        "stelvio:aws:Function",
    ),
    ("Layer", lambda: Layer("test-layer", requirements=["requests"]), "stelvio:aws:Layer"),
    ("Url", lambda: Url("test-url", "https://example.com"), "stelvio:aws:Url"),
]


@pytest.mark.parametrize(
    ("name", "factory", "expected_urn"),
    SIMPLE_COMPONENTS,
    ids=[c[0] for c in SIMPLE_COMPONENTS],
)
@pulumi.runtime.test
def test_type_urn_registered_at_runtime(pulumi_mocks, name, factory, expected_urn):
    """Instantiating a component creates a resource with the correct type URN."""
    component = factory()

    def check(_):
        stelvio_resources = [r for r in pulumi_mocks.created_resources if r.typ == expected_urn]
        assert len(stelvio_resources) == 1, (
            f"Expected 1 resource with type '{expected_urn}', found {len(stelvio_resources)}"
        )

    component.urn.apply(check)
