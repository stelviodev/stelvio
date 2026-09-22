"""Opens a TLS Mongo connection using the documented DocumentDB Lambda path.

Used by the DocumentDB linked-Function integration test. The cluster is
accessed via the 'todos' link. Stelvio packages Amazon's CA bundle into the
Lambda; its path is already in the URI. The password comes from Secrets Manager.
"""

import json
from urllib.parse import urlsplit

import boto3
from pymongo import MongoClient
from stlv_resources import Resources

_TIMEOUT_MS = 20000

secrets = boto3.client("secretsmanager")


def _password() -> str:
    secret = json.loads(
        secrets.get_secret_value(SecretId=Resources.todos.secret_arn)["SecretString"]
    )
    return secret["password"]


def _writer(password: str) -> MongoClient:
    return MongoClient(
        Resources.todos.connection_uri,
        username=Resources.todos.username,
        password=password,
        serverSelectionTimeoutMS=_TIMEOUT_MS,
    )


def _reader(password: str) -> MongoClient:
    query = urlsplit(Resources.todos.connection_uri).query
    uri = f"mongodb://{Resources.todos.reader_host}:{Resources.todos.port}/?{query}"
    return MongoClient(
        uri,
        username=Resources.todos.username,
        password=password,
        serverSelectionTimeoutMS=_TIMEOUT_MS,
    )


def main(event, context):
    password = _password()
    client = _writer(password)
    try:
        collection = client.stelvio_test.documents
        if event.get("operation") == "write":
            document = event["document"]
            collection.replace_one({"_id": document["_id"]}, document, upsert=True)
            return {"document": collection.find_one({"_id": document["_id"]})}
        if event.get("operation") == "read":
            return {"document": collection.find_one({"_id": event["id"]})}
        ping = client.admin.command("ping")
        reader = _reader(password)
        try:
            reader_ping = reader.admin.command("ping")
        finally:
            reader.close()
        return {
            "username": Resources.todos.username,
            "mongo_ok": ping.get("ok") == 1 and reader_ping.get("ok") == 1,
        }
    finally:
        client.close()
