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
    S3BucketResources,
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
    ("resources_type", "customization_dict_type", "excluded_resource_fields"),
    [
        pytest.param(
            FunctionResources,
            FunctionCustomizationDict,
            None,
            id="Function",
        ),
        pytest.param(
            S3BucketResources,
            BucketCustomizationDict,
            None,
            id="Bucket",
        ),
        pytest.param(
            BucketNotifySubscriptionResources,
            BucketNotifySubscriptionCustomizationDict,
            None,
            id="BucketNotifySubscription",
        ),
        pytest.param(
            QueueResources,
            QueueCustomizationDict,
            None,
            id="Queue",
        ),
        pytest.param(
            QueueSubscriptionResources,
            QueueSubscriptionCustomizationDict,
            None,
            id="QueueSubscription",
        ),
        pytest.param(
            TopicResources,
            TopicCustomizationDict,
            None,
            id="Topic",
        ),
        pytest.param(
            TopicSubscriptionResources,
            TopicSubscriptionCustomizationDict,
            None,
            id="TopicSubscription",
        ),
        pytest.param(
            TopicQueueSubscriptionResources,
            TopicQueueSubscriptionCustomizationDict,
            None,
            id="TopicQueueSubscription",
        ),
        pytest.param(
            DynamoTableResources,
            DynamoTableCustomizationDict,
            None,
            id="DynamoTable",
        ),
        pytest.param(
            DynamoSubscriptionResources,
            DynamoSubscriptionCustomizationDict,
            None,
            id="DynamoSubscription",
        ),
        pytest.param(
            CronResources,
            CronCustomizationDict,
            None,
            id="Cron",
        ),
        pytest.param(
            EmailResources,
            EmailCustomizationDict,
            None,
            id="Email",
        ),
        pytest.param(
            ApiResources,
            ApiCustomizationDict,
            None,
            id="Api",
        ),
        pytest.param(
            AcmValidatedDomainResources,
            AcmValidatedDomainCustomizationDict,
            None,
            id="AcmValidatedDomain",
        ),
        pytest.param(
            LayerResources,
            LayerCustomizationDict,
            None,
            id="Layer",
        ),
        pytest.param(
            CloudFrontDistributionResources,
            CloudFrontDistributionCustomizationDict,
            # function_associations is config data passed to distribution, not a
            # standalone Pulumi resource
            {"function_associations"},
            id="CloudFrontDistribution",
        ),
        pytest.param(
            RouterResources,
            RouterCustomizationDict,
            None,
            id="Router",
        ),
        pytest.param(
            S3StaticWebsiteResources,
            S3StaticWebsiteCustomizationDict,
            None,
            id="S3StaticWebsite",
        ),
    ],
)
def test_resources_matches_customization_dict(
    resources_type, customization_dict_type, excluded_resource_fields
):
    """Verify that Resources dataclass fields match CustomizationDict keys."""
    assert_resources_matches_customization_dict(
        resources_type,
        customization_dict_type,
        excluded_resource_fields=excluded_resource_fields,
    )
