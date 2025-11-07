from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import Api
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("stlvapp")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            # region="us-east-1",        # Uncomment to override AWS CLI/env var region
            # profile="your-profile",    # Uncomment to use specific AWS profile
        ),
    )


@app.run
def run() -> None:
    # API Gateway with the messages table linked
    api = Api("my-api")
    api.route("GET", "/", "functions/api.handler")
