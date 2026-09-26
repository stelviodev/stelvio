from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.queue import Queue
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e6-fanout-topic")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    queue = Queue("order-events")
    api = HttpApi("orders-api")
    api.route("POST", "/orders/complete", "functions/complete.handler", links=[queue])
    queue.subscribe("email", "functions/email.handler")
    queue.subscribe("analytics", "functions/analytics.handler")
    queue.subscribe("inventory", "functions/inventory.handler")
