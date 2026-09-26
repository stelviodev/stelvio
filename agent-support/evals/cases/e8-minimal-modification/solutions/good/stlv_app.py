from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.topic import Topic
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e8-minimal-modification")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    users = DynamoTable(
        "users",
        fields={"id": "string"},
        partition_key="id",
    )
    topic = Topic("user-created")
    api = HttpApi("users-api")
    api.route("POST", "/users", "functions/users.create", links=[users, topic])
