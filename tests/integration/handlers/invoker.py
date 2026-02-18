"""Invokes a linked Lambda function and returns its response.

Used by Function->Function scenario test. The target function is
accessed via the 'target' link.
"""

import json

import boto3
from stlv_resources import Resources


def main(event, context):
    client = boto3.client("lambda")

    resp = client.invoke(
        FunctionName=Resources.target.function_arn,
        Payload=json.dumps(event),
    )
    return json.loads(resp["Payload"].read())
