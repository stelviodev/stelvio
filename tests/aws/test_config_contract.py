from collections.abc import Callable
from typing import Any

import pytest

from stelvio.aws.api_gateway import HttpApi, RestApi, WebsocketApi
from stelvio.aws.appsync import AppSync
from stelvio.aws.cognito.identity_pool import IdentityPool
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.cognito.user_pool_client import UserPoolClient
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.email import Email
from stelvio.aws.function import Function
from stelvio.aws.layer import Layer
from stelvio.aws.queue import Queue

BAD = 42  # not a str: Email reads a str config as its sender

CASES = [
    pytest.param(lambda: Function("fn", config=BAD), id="Function"),
    pytest.param(lambda: Layer("layer", config=BAD), id="Layer"),
    pytest.param(lambda: Queue("queue", config=BAD), id="Queue"),
    pytest.param(lambda: DynamoTable("table", config=BAD), id="DynamoTable"),
    pytest.param(lambda: Email("email", config=BAD), id="Email"),
    pytest.param(lambda: UserPool("pool", config=BAD), id="UserPool"),
    pytest.param(
        lambda: UserPoolClient("client", pool=UserPool("pool"), config=BAD), id="UserPoolClient"
    ),
    pytest.param(lambda: IdentityPool("identity", config=BAD), id="IdentityPool"),
    pytest.param(lambda: RestApi("rest", config=BAD), id="RestApi"),
    pytest.param(lambda: HttpApi("http", config=BAD), id="HttpApi"),
    pytest.param(lambda: WebsocketApi("ws", config=BAD), id="WebsocketApi"),
    pytest.param(lambda: AppSync("graphql", config=BAD), id="AppSync"),
]


@pytest.mark.parametrize("build", CASES)
def test_config_of_wrong_type_raises_type_error(pulumi_mocks, build: Callable[[], Any]):
    with pytest.raises(
        TypeError, match=r"Invalid config type: expected \w+Config or dict, got int"
    ):
        build()
