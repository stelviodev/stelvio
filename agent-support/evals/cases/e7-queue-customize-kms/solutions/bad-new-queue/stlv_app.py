from stelvio.app import StelvioApp
from stelvio.aws.queue import Queue
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e7-queue-customize-kms")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    # Replaced the existing component with a differently named queue.
    Queue(
        "orders-encrypted",
        customize={
            "queue": {
                "kms_master_key_id": "alias/orders",
            }
        },
    )
