from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig

from stelvio.aws.function import Function

from stelvio.aws.bedrock.agents import Agent

app = StelvioApp("arr-agent-app")


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

    lambda_function = Function(
        name="my-function",
        handler="agents/get_current_time.lambda_handler",
    )

    arr_agent = Agent(
        name="my-arr-agent",
        function=lambda_function,
    )
