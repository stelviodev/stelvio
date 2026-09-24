"""Opens a TLS Mongo connection the way the DocumentDB guide shows.

Used by the DocumentDB linked-Function integration tests. The cluster is
accessed via the 'todos' link. Stelvio packages Amazon's CA bundle into the
Lambda; its path is already in the URI. The password comes from Secrets Manager,
and clients live at module level so warm invocations reuse them.
"""

import json
from urllib.parse import urlsplit

import boto3
from pymongo import MongoClient
from pymongo.errors import OperationFailure
from stlv_resources import Resources

AUTHENTICATION_FAILED = 18
_TIMEOUT_MS = 20000

secrets = boto3.client("secretsmanager")


def _connect(uri: str) -> MongoClient:
    secret = secrets.get_secret_value(SecretId=Resources.todos.secret_arn)
    return MongoClient(
        uri,
        username=Resources.todos.username,
        password=json.loads(secret["SecretString"])["password"],
        serverSelectionTimeoutMS=_TIMEOUT_MS,
    )


def _reader_uri() -> str:
    query = urlsplit(Resources.todos.connection_uri).query
    return f"mongodb://{Resources.todos.reader_host}:{Resources.todos.port}/?{query}"


clients = {
    "writer": _connect(Resources.todos.connection_uri),
    "reader": _connect(_reader_uri()),
}


def _with_reconnect(role: str, uri: str, operation):
    try:
        return operation(clients[role])
    except OperationFailure as error:
        if error.code != AUTHENTICATION_FAILED:
            raise
        clients[role].close()
        clients[role] = _connect(uri)
        return operation(clients[role])


def main(event, context):
    writer_uri = Resources.todos.connection_uri
    if event.get("operation") == "write":
        document = event["document"]
        collection = clients["writer"].stelvio_test.documents
        collection.replace_one({"_id": document["_id"]}, document, upsert=True)
        return {"document": collection.find_one({"_id": document["_id"]})}
    if event.get("operation") == "read":
        document = _with_reconnect(
            "writer",
            writer_uri,
            lambda client: client.stelvio_test.documents.find_one({"_id": event["id"]}),
        )
        return {"document": document}
    ping = _with_reconnect("writer", writer_uri, lambda c: c.admin.command("ping"))
    reader_ping = _with_reconnect("reader", _reader_uri(), lambda c: c.admin.command("ping"))
    return {
        "username": Resources.todos.username,
        "mongo_ok": ping.get("ok") == 1 and reader_ping.get("ok") == 1,
    }
