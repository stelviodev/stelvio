from stelvio.app import StelvioApp
from stelvio.aws.s3 import Bucket
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e5-s3-notify-queue")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    bucket = Bucket("videos")
    bucket.notify_function(
        "worker",
        events=["s3:ObjectCreated:*"],
        function="functions/worker.handler",
    )
