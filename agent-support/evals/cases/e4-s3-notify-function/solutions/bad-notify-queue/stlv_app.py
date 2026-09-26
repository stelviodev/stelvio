from stelvio.app import StelvioApp
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e4-s3-notify-function")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    bucket = Bucket("uploads")
    queue = Queue("images")
    bucket.notify_queue(
        "to-queue",
        events=["s3:ObjectCreated:*"],
        filter_prefix="incoming/",
        filter_suffix=".jpg",
        queue=queue,
    )
