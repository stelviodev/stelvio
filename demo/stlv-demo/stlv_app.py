#!/usr/bin/env python3
from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import Api
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("stlv-demo")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            region="us-east-1",  # Uncomment to override AWS CLI/env var region
            # profile="your-profile",    # Uncomment to use specific AWS profile
        ),
    )


@app.run
def run() -> None:
    todos = DynamoTable(
        "todos-table",
        fields={"user": "string", "date": "string"},
        sort_key="date",
        partition_key="user",
    )

    # Disabled to avoid incurring costs when forgot to destroy
    # cleanup = Cron(
    #     "cleanup-cron",
    #     "rate(1 minute)",
    #     handler="api/handlers.cleanup",
    #     links=[todos]
    # )

    api = Api("stlv-demo-api")
    api.route("GET", "/hello", handler="api/handlers.hello_world")
    api.route("POST", "/todos", handler="api/handlers.post_todo", links=[todos])
    api.route("GET", "/todos/{user}", handler="api/handlers.list_todos", links=[todos])
