import inspect

from pytest import mark

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway import ApiDomain
from stelvio.aws.appsync.data_source import AppSyncDataSource
from stelvio.aws.appsync.resolver import AppSyncResolver, PipeFunction
from stelvio.aws.cloudfront.cloudfront import CloudFrontDistribution
from stelvio.aws.cloudfront.origins.components.url import Url
from stelvio.aws.cognito.identity_provider import IdentityProvider
from stelvio.aws.cognito.user_pool_client import UserPoolClient
from stelvio.aws.dynamo_db import DynamoSubscription
from stelvio.aws.function import Function
from stelvio.aws.layer import Layer
from stelvio.aws.queue import QueueSubscription
from stelvio.aws.s3.s3 import Bucket, BucketNotifySubscription
from stelvio.aws.topic import TopicQueueSubscription, TopicSubscription
from tests.test_utils import all_component_classes

# Pinned sets. A member that grows or loses the parameter fails below, so they can't go stale.

# Nothing these create takes tags on AWS.
TAGLESS = {
    AppSyncResolver,
    IdentityProvider,
    Layer,
    PipeFunction,
    TopicQueueSubscription,
    UserPoolClient,
}
# Creates no AWS resource, so there is nothing to customize.
NO_CUSTOMIZE = {Url}
# Other components create these as children and pass `parent=`.
TAKES_PARENT = {
    AcmValidatedDomain,
    ApiDomain,
    AppSyncDataSource,
    AppSyncResolver,
    Bucket,
    BucketNotifySubscription,
    CloudFrontDistribution,
    DynamoSubscription,
    Function,
    IdentityProvider,
    PipeFunction,
    QueueSubscription,
    TopicQueueSubscription,
    TopicSubscription,
    UserPoolClient,
}


@mark.parametrize("cls", all_component_classes(), ids=lambda cls: cls.__name__)
def test_component_takes_tags_customize_and_parent_keyword_only(cls):
    params = inspect.signature(cls.__init__).parameters
    expected = {
        "tags": cls not in TAGLESS,
        "customize": cls not in NO_CUSTOMIZE,
        "parent": cls in TAKES_PARENT,
    }
    for name, takes in expected.items():
        if takes:
            assert name in params
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        else:
            assert name not in params
