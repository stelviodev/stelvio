from stelvio.app import StelvioApp
from stelvio.aws.queue import Queue
from stelvio.aws.s3 import Bucket
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e5-s3-notify-queue")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    bucket = Bucket("videos")
    queue = Queue("video-processing")
    bucket.notify_queue(
        "to-queue",
        events=["s3:ObjectCreated:*"],
        queue=queue,
    )
    queue.subscribe("worker", "functions/worker.handler")
