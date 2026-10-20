"""DocumentDB probe executed by a local handler in the opt-in dev tunnel scenario."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import fields
from pathlib import Path

import boto3
from pymongo import MongoClient
from stlv_resources import Resources

_invocation_path = os.environ.get("STLV_TEST_TUNNEL_INVOCATIONS")
if _invocation_path:
    with Path(_invocation_path).open("a") as stream:
        stream.write(json.dumps({"pid": os.getpid()}) + "\n")


def handler(_event: object, context: object) -> dict[str, object]:
    database = next(
        getattr(Resources, item.name)
        for item in fields(Resources)
        if hasattr(type(getattr(Resources, item.name)), "secret_arn")
    )
    secret = boto3.client("secretsmanager").get_secret_value(SecretId=database.secret_arn)
    password = json.loads(secret["SecretString"])["password"]
    with MongoClient(
        database.connection_uri,
        username=database.username,
        password=password,
        tls=True,
        tlsCAFile=database.ca_file,
        replicaSet="rs0",
        retryWrites=False,
        serverSelectionTimeoutMS=20_000,
    ) as client:
        ping = client.admin.command("ping")
        marker = uuid.uuid4().hex
        collection = client.stelvio_dev.acceptance
        collection.replace_one({"_id": marker}, {"_id": marker}, upsert=True)
        read_back = collection.find_one({"_id": marker})
        body = {
            "tls": ping.get("ok") == 1,
            "read_write": bool(read_back and read_back.get("_id") == marker),
            "expected_host": database.host,
            "members": sorted(host for host, _ in client.nodes),
            "pid": os.getpid(),
            "request_id": getattr(context, "aws_request_id", None),
        }
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }
