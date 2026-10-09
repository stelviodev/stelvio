"""Verify that all Stelvio component type URNs are correct and complete.

Maintains a canonical mapping of every Component subclass to its expected
Pulumi type URN. Guards against typos, inconsistent naming, and new
components being added without updating this list.
"""

import re

import pulumi
import pytest

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway import ApiDomain, HttpApi, RestApi, WebsocketApi
from stelvio.aws.appsync import AppSync
from stelvio.aws.appsync.data_source import AppSyncDataSource
from stelvio.aws.appsync.resolver import AppSyncResolver, PipeFunction
from stelvio.aws.cloudfront.cloudfront import CloudFrontDistribution
from stelvio.aws.cloudfront.origins.components.url import Url
from stelvio.aws.cloudfront.router import Router
from stelvio.aws.cognito.identity_pool import IdentityPool
from stelvio.aws.cognito.identity_provider import IdentityProvider
from stelvio.aws.cognito.types import IdentityPoolBinding
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.cognito.user_pool_client import UserPoolClient
from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoSubscription, DynamoTable
from stelvio.aws.email import Email
from stelvio.aws.function.function import Function
from stelvio.aws.layer import Layer
from stelvio.aws.queue import Queue, QueueSubscription
from stelvio.aws.s3.s3 import Bucket, BucketNotifySubscription
from stelvio.aws.s3.s3_static_website import S3StaticWebsite
from stelvio.aws.topic import Topic, TopicQueueSubscription, TopicSubscription
from stelvio.aws.vpc import Vpc
from stelvio.component import Component
from tests.test_utils import all_component_classes

# Canonical mapping: every Component subclass → its expected type URN.
# If you add a new component, add it here too.
CANONICAL_URNS: dict[type[Component], str] = {
    Function: "stelvio:aws:Function",
    RestApi: "stelvio:aws:RestApi",
    HttpApi: "stelvio:aws:HttpApi",
    WebsocketApi: "stelvio:aws:WebsocketApi",
    ApiDomain: "stelvio:aws:ApiDomain",
    AppSync: "stelvio:aws:AppSync",
    AppSyncDataSource: "stelvio:aws:AppSyncDataSource",
    AppSyncResolver: "stelvio:aws:AppSyncResolver",
    PipeFunction: "stelvio:aws:PipeFunction",
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
    UserPool: "stelvio:aws:UserPool",
    UserPoolClient: "stelvio:aws:UserPoolClient",
    IdentityProvider: "stelvio:aws:IdentityProvider",
    IdentityPool: "stelvio:aws:IdentityPool",
    Vpc: "stelvio:aws:Vpc",
}


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


def test_canonical_list_entry_count():
    """Pin the registered component type count."""
    assert len(CANONICAL_URNS) == 31


def test_canonical_list_is_complete():
    """Every Component subclass in stelvio.aws is in the canonical list, and discovery finds
    every listed one.

    Catches new components being added without updating this test file, and a broken
    discovery, which the keyword-only suite depends on.
    """
    assert set(all_component_classes()) == set(CANONICAL_URNS)


def test_no_duplicate_urns():
    """All type URN strings are unique across components."""
    urns = list(CANONICAL_URNS.values())
    assert len(urns) == len(set(urns)), (
        f"Duplicate URNs found: {[u for u in urns if urns.count(u) > 1]}"
    )


# =========================================================================
# Runtime verification (simple components only)
# =========================================================================

SIMPLE_COMPONENTS = [
    ("RestApi", lambda: RestApi("test-api"), "stelvio:aws:RestApi"),
    (
        "AppSync",
        lambda: AppSync("test-appsync", schema="type Query { ok: String }", auth="iam"),
        "stelvio:aws:AppSync",
    ),
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
    (
        "UserPool",
        lambda: UserPool("test-pool", usernames=["email"]),
        "stelvio:aws:UserPool",
    ),
    (
        "IdentityPool",
        lambda: IdentityPool(
            "test-identity",
            user_pools=[
                IdentityPoolBinding(user_pool="us-east-1_test", client="test-client"),
            ],
        ),
        "stelvio:aws:IdentityPool",
    ),
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
