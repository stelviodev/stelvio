from stelvio.app import StelvioApp
from stelvio.aws.s3 import Bucket
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e4-s3-notify-function")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    bucket = Bucket("uploads")
    bucket.notify_function(
        "process-image",
        events=["s3:ObjectCreated:*"],
        filter_prefix="uploads/",
        filter_suffix=".png",
        function="functions/process_image.handler",
    )
