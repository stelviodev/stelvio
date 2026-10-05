from stelvio.app import StelvioApp
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import NatConfig, Vpc
from stelvio.config import AwsConfig, StelvioAppConfig

app = StelvioApp("vpc-tunnel")


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
    # Private subnets need a route to Secrets Manager for the managed password.
    # bastion=True is the dev opt-in: DocumentDB then admits that security group
    # on the cluster port. It does not open a workstation path by itself.
    vpc = Vpc("net", nat=NatConfig(type="managed", single=True), bastion=True)
    docdb = DocumentDb("docdb", vpc=vpc)
    Function(
        "api",
        handler="functions/ping.handler",
        requirements=["pymongo"],
        url="public",
        vpc=vpc,
        links=[docdb],
        memory=256,
    )
