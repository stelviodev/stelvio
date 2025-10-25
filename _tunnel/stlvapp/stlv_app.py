from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import Api
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.config import StelvioAppConfig, AwsConfig

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
    # Create DynamoDB table for real-time messaging with streams enabled
    messages_table = DynamoTable(
        "messages",
        fields={
            "id": "string",
            "timestamp": "string"
        },
        partition_key="id",
        sort_key="timestamp",
        stream="keys-only"  # Enable streams for real-time processing
    )

    # Subscribe a function to process stream events (echoing messages)
    # messages_table.subscribe("echo-processor", "functions/echo_processor.handler")

    # API Gateway with the messages table linked
    api = Api('my-api')
    api.route('GET', '/', 'functions/api.handler', links=[messages_table])
    

