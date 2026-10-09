import inspect

from pytest import mark

from stelvio.aws.appsync.resolver import AppSyncResolver, PipeFunction
from stelvio.aws.cloudfront.origins.components.url import Url
from stelvio.aws.cognito.identity_provider import IdentityProvider
from stelvio.aws.cognito.user_pool_client import UserPoolClient
from stelvio.aws.layer import Layer
from stelvio.aws.topic import TopicQueueSubscription
from tests.test_utils import all_component_classes

# Nothing these create takes tags on AWS. A member that grows `tags` fails below, so the set
# can't go stale.
TAGLESS = {
    AppSyncResolver,
    IdentityProvider,
    Layer,
    PipeFunction,
    TopicQueueSubscription,
    UserPoolClient,
}
NO_CUSTOMIZE = {Url}


@mark.parametrize("cls", all_component_classes(), ids=lambda cls: cls.__name__)
def test_component_takes_tags_customize_and_parent_keyword_only(cls):
    params = inspect.signature(cls.__init__).parameters

    assert ("tags" in params) is (cls not in TAGLESS)
    assert ("customize" in params) is (cls not in NO_CUSTOMIZE)
    positional = [
        name
        for name in ("tags", "customize", "parent")
        if name in params and params[name].kind is not inspect.Parameter.KEYWORD_ONLY
    ]
    assert positional == []
