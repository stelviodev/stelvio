from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e2-missing-link")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    DynamoTable(
        "users",
        fields={"id": "string"},
        partition_key="id",
    )
    api = HttpApi("users-api")
    # Table exists and handler uses Resources, but the route is not linked.
    api.route("GET", "/users/{id}", "functions/users.get")
