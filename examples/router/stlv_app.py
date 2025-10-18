from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig

from stelvio.aws.s3.s3 import Bucket
from stelvio.aws.apigateway.api import Api

app = StelvioApp("router")

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
    domain_name = "rtr.r53.ectlnet.com"
    """
        /api/hello  --> Lambda function that returns "Hello, World!" via API Gateway
        /files/*   --> S3 bucket to serve static files
    """
    bucket = Bucket("static-files-bucket", access="public")
    api = Api('my-api')
    api.route('GET', '/hello', 'functions/hello.handler')

    router = CloudfrontRouter(root_domain=domain_name, root_path="/")
    router.add("/api", api)
    router.add("/files", bucket)
