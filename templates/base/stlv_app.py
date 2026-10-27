from stelvio.app import StelvioApp
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("stelvio-template")


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
    # Create your infra here
    pass
