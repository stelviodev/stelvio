"""Tests to verify *Resources dataclasses stay in sync with *CustomizationDict TypedDicts.

These tests ensure that when a new resource is added to a component's Resources
dataclass, a corresponding key is also added to the CustomizationDict TypedDict.
This prevents silent failures where users try to customize resources that don't
have customization support.
"""

import pytest

# Import all Resources and CustomizationDict pairs
from stelvio.aws.acm import AcmValidatedDomainCustomizationDict, AcmValidatedDomainResources
from stelvio.aws.api_gateway.api import ApiCustomizationDict, ApiResources
from stelvio.aws.cloudfront.cloudfront import (
    CloudFrontDistributionCustomizationDict,
    CloudFrontDistributionResources,
)
from stelvio.aws.cloudfront.router import RouterCustomizationDict, RouterResources
from stelvio.aws.cron import CronCustomizationDict, CronResources
from stelvio.aws.dynamo_db import (
    DynamoSubscriptionCustomizationDict,
    DynamoSubscriptionResources,
    DynamoTableCustomizationDict,
    DynamoTableResources,
)
from stelvio.aws.email import EmailCustomizationDict, EmailResources
from stelvio.aws.function.function import FunctionCustomizationDict, FunctionResources
from stelvio.aws.layer import LayerCustomizationDict, LayerResources
from stelvio.aws.queue import (
    QueueCustomizationDict,
    QueueResources,
    QueueSubscriptionCustomizationDict,
    QueueSubscriptionResources,
)
from stelvio.aws.s3.s3 import (
    BucketCustomizationDict,
    BucketNotifySubscriptionCustomizationDict,
    BucketNotifySubscriptionResources,
    BucketResources,
)
from stelvio.aws.s3.s3_static_website import (
    S3StaticWebsiteCustomizationDict,
    S3StaticWebsiteResources,
)
from stelvio.aws.topic import (
    TopicCustomizationDict,
    TopicQueueSubscriptionCustomizationDict,
    TopicQueueSubscriptionResources,
    TopicResources,
    TopicSubscriptionCustomizationDict,
    TopicSubscriptionResources,
)
from tests.test_utils import assert_resources_matches_customization_dict


@pytest.mark.parametrize(
    (
        "resources_type",
        "customization_dict_type",
        "excluded_resource_fields",
        "excluded_customization_keys",
    ),
    [
        pytest.param(
            FunctionResources,
            FunctionCustomizationDict,
            None,
            None,
            id="Function",
        ),
        pytest.param(
            BucketResources,
            BucketCustomizationDict,
            None,
            # function, queue, topic are notification config blocks, not standalone resources
            {"function", "queue", "topic"},
            id="Bucket",
        ),
        pytest.param(
            BucketNotifySubscriptionResources,
            BucketNotifySubscriptionCustomizationDict,
            None,
            None,
            id="BucketNotifySubscription",
        ),
        pytest.param(
            QueueResources,
            QueueCustomizationDict,
            None,
            None,
            id="Queue",
        ),
        pytest.param(
            QueueSubscriptionResources,
            QueueSubscriptionCustomizationDict,
            None,
            None,
            id="QueueSubscription",
        ),
        pytest.param(
            TopicResources,
            TopicCustomizationDict,
            None,
            None,
            id="Topic",
        ),
        pytest.param(
            TopicSubscriptionResources,
            TopicSubscriptionCustomizationDict,
            None,
            None,
            id="TopicSubscription",
        ),
        pytest.param(
            TopicQueueSubscriptionResources,
            TopicQueueSubscriptionCustomizationDict,
            None,
            None,
            id="TopicQueueSubscription",
        ),
        pytest.param(
            DynamoTableResources,
            DynamoTableCustomizationDict,
            None,
            None,
            id="DynamoTable",
        ),
        pytest.param(
            DynamoSubscriptionResources,
            DynamoSubscriptionCustomizationDict,
            None,
            None,
            id="DynamoSubscription",
        ),
        pytest.param(
            CronResources,
            CronCustomizationDict,
            None,
            None,
            id="Cron",
        ),
        pytest.param(
            EmailResources,
            EmailCustomizationDict,
            None,
            None,
            id="Email",
        ),
        pytest.param(
            ApiResources,
            ApiCustomizationDict,
            None,
            None,
            id="Api",
        ),
        pytest.param(
            AcmValidatedDomainResources,
            AcmValidatedDomainCustomizationDict,
            None,
            None,
            id="AcmValidatedDomain",
        ),
        pytest.param(
            LayerResources,
            LayerCustomizationDict,
            None,
            None,
            id="Layer",
        ),
        pytest.param(
            CloudFrontDistributionResources,
            CloudFrontDistributionCustomizationDict,
            # function_associations is config data passed to distribution, not a
            # standalone Pulumi resource
            {"function_associations"},
            None,
            id="CloudFrontDistribution",
        ),
        pytest.param(
            RouterResources,
            RouterCustomizationDict,
            None,
            None,
            id="Router",
        ),
        pytest.param(
            S3StaticWebsiteResources,
            S3StaticWebsiteCustomizationDict,
            None,
            None,
            id="S3StaticWebsite",
        ),
    ],
)
def test_resources_matches_customization_dict(
    resources_type,
    customization_dict_type,
    excluded_resource_fields,
    excluded_customization_keys,
):
    """Verify that Resources dataclass fields match CustomizationDict keys."""
    assert_resources_matches_customization_dict(
        resources_type,
        customization_dict_type,
        excluded_resource_fields=excluded_resource_fields,
        excluded_customization_keys=excluded_customization_keys,
    )
