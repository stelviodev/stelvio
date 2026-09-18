"""Opens a TLS Mongo connection with the linked DocumentDB connection_string.

Used by the DocumentDB linked-Function integration test. The cluster is
accessed via the 'todos' link. Stelvio packages Amazon's CA bundle into the
Lambda; its path is already in the URI.
"""

from pymongo import MongoClient
from stlv_resources import Resources

_TIMEOUT_MS = 20000


def _writer() -> MongoClient:
    return MongoClient(Resources.todos.connection_string, serverSelectionTimeoutMS=_TIMEOUT_MS)


def _reader() -> MongoClient:
    uri = Resources.todos.connection_string.replace(
        Resources.todos.host, Resources.todos.reader_host, 1
    )
    return MongoClient(uri, serverSelectionTimeoutMS=_TIMEOUT_MS)


def main(event, context):
    client = _writer()
    try:
        collection = client.stelvio_test.documents
        if event.get("operation") == "write":
            document = event["document"]
            collection.replace_one({"_id": document["_id"]}, document, upsert=True)
            return {"document": collection.find_one({"_id": document["_id"]})}
        if event.get("operation") == "read":
            return {"document": collection.find_one({"_id": event["id"]})}
        ping = client.admin.command("ping")
        reader = _reader()
        try:
            reader_ping = reader.admin.command("ping")
        finally:
            reader.close()
        return {
            "has_password": "@" in Resources.todos.connection_string.split("://", 1)[-1],
            "username": Resources.todos.username,
            "mongo_ok": ping.get("ok") == 1 and reader_ping.get("ok") == 1,
        }
    finally:
        client.close()
