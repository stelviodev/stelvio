import inspect

import pytest

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway.api import Api, _create_custom_domain
from stelvio.aws.cloudfront.cloudfront import CloudFrontDistribution
from stelvio.aws.cloudfront.origins.components.url import Url
from stelvio.aws.cloudfront.router import Router
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.cognito.user_pool_client import UserPoolClient
from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoSubscription, DynamoTable
from stelvio.aws.email import Email
from stelvio.aws.function import Function
from stelvio.aws.queue import Queue, QueueSubscription
from stelvio.aws.s3.s3 import Bucket, BucketNotifySubscription
from stelvio.aws.s3.s3_static_website import S3StaticWebsite
from stelvio.aws.topic import Topic, TopicQueueSubscription, TopicSubscription


@pytest.mark.parametrize(
    ("callable_obj", "param_name"),
    [
        (Function.__init__, "tags"),
        (Function.__init__, "customize"),
        (Api.__init__, "tags"),
        (Api.__init__, "customize"),
        (Email.__init__, "tags"),
        (Email.__init__, "customize"),
        (AcmValidatedDomain.__init__, "tags"),
        (AcmValidatedDomain.__init__, "customize"),
        (Router.__init__, "tags"),
        (Router.__init__, "customize"),
        (CloudFrontDistribution.__init__, "tags"),
        (CloudFrontDistribution.__init__, "customize"),
        (Bucket.__init__, "tags"),
        (Bucket.__init__, "customize"),
        (S3StaticWebsite.__init__, "tags"),
        (S3StaticWebsite.__init__, "customize"),
        (Url.__init__, "tags"),
        (Queue.__init__, "tags"),
        (Queue.__init__, "customize"),
        (QueueSubscription.__init__, "tags"),
        (QueueSubscription.__init__, "customize"),
        (DynamoTable.__init__, "tags"),
        (DynamoTable.__init__, "customize"),
        (DynamoSubscription.__init__, "tags"),
        (DynamoSubscription.__init__, "customize"),
        (Topic.__init__, "tags"),
        (Topic.__init__, "customize"),
        (BucketNotifySubscription.__init__, "tags"),
        (BucketNotifySubscription.__init__, "customize"),
        (TopicSubscription.__init__, "tags"),
        (TopicSubscription.__init__, "customize"),
        (Cron.__init__, "tags"),
        (Cron.__init__, "customize"),
        (UserPool.__init__, "tags"),
        (UserPool.__init__, "customize"),
        (UserPoolClient.__init__, "tags"),
        (UserPoolClient.__init__, "customize"),
        (_create_custom_domain, "tags"),
    ],
)
def test_params_are_keyword_only(callable_obj, param_name):
    signature = inspect.signature(callable_obj)
    assert signature.parameters[param_name].kind is inspect.Parameter.KEYWORD_ONLY


def test_topic_queue_subscription_has_no_tags_param():
    signature = inspect.signature(TopicQueueSubscription.__init__)
    assert "tags" not in signature.parameters
    assert signature.parameters["customize"].kind is inspect.Parameter.KEYWORD_ONLY
