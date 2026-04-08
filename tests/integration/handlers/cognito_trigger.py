"""Cognito pre-sign-up trigger that records the event and auto-confirms the user.

Used by Cognito e2e trigger tests. Deployed with links=[results_table],
writes the trigger event to DynamoDB, then auto-confirms the user so the
sign-up flow completes without manual verification.
"""

import json
import time

import boto3
from stlv_resources import Resources


def pre_sign_up(event, context):
    table = boto3.resource("dynamodb").Table(Resources.results.table_name)

    table.put_item(
        Item={
            "pk": context.aws_request_id,
            "event": json.dumps(event, default=str),
            "source": context.function_name,
            "timestamp": str(time.time()),
        }
    )

    # Auto-confirm so sign-up succeeds without email verification
    event["response"]["autoConfirmUser"] = True
    return event
