from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import HttpApi
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e1-linked-dynamodb")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    api = HttpApi("users-api")
    api.route("GET", "/users/{id}", "functions/users.get")
