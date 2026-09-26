from stelvio.app import StelvioApp
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("e4-s3-notify-function")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig())


@app.run
def run() -> None:
    pass
