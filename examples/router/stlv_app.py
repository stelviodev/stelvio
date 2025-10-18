import pulumi
import pulumi_aws

from stelvio.app import StelvioApp
from stelvio.aws.cloudfront import CloudfrontRouter
from stelvio.aws.dns import Route53Dns
from stelvio.config import StelvioAppConfig, AwsConfig

from stelvio.aws.s3.s3 import Bucket
# from stelvio.aws.apigateway.api import Api

app = StelvioApp("router")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            # region="us-east-1",        # Uncomment to override AWS CLI/env var region
            # profile="your-profile",    # Uncomment to use specific AWS profile
        ),
        dns=Route53Dns(zone_id="Z08488092RCBV4ZZV4EJ8")
    )


@app.run
def run() -> None:
    domain_name = "rtr.r53.ectlnet.com"
    """
        /api/hello  --> Lambda function that returns "Hello, World!" via API Gateway
        /files/*   --> S3 bucket to serve static files
    """
    bucket = Bucket("static-files-bucket", access="public")
    bucket_object = pulumi_aws.s3.BucketObject(
        "example-object",
        bucket=bucket.resources.bucket.id,
        key="hello.txt",
        content="Hello, World!",
    )
    pulumi.export(f"s3bucket_{bucket.name}_object_id", bucket_object.id)


    # api = Api("my-api")
    # api.route("GET", "/hello", "functions/hello.handler")

    router = CloudfrontRouter("rtr-test", custom_domain=domain_name)
    router.route("*", "/files", bucket)


