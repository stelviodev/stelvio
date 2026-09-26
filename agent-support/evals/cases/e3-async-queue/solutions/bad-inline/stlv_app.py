from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import HttpApi
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e3-async-queue")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    api = HttpApi("orders-api")
    api.route("POST", "/orders", "functions/orders.accept")
