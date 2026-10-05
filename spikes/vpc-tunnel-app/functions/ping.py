"""TLS read/write proof against the linked DocumentDB cluster.

`stlv deploy` and `stlv dev` both run this handler. Link properties arrive as
`stlv_resources.Resources.docdb` (the `STLV_DOCDB_*` environment variables).
The URI has no password. TLS stays on, and hostname checks stay on.
"""

from __future__ import annotations

import json
import re

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from pymongo import MongoClient
from pymongo.errors import InvalidOperation, OperationFailure, PyMongoError
from stlv_resources import Resources

_AUTHENTICATION_FAILED = 18
_HTTP_OK = 200
_HTTP_ERROR = 500
_PING_OK = 1.0
_SELECTION_TIMEOUT_MS = 20_000
_DOCDB_SUFFIX = ".docdb.amazonaws.com"
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_DATABASE = "stelvio"
_COLLECTION = "tunnel_proof"
_DOC_ID = "proof"
_MARKER = "ok"
_ERROR_LIMIT = 500

_URI_RE = re.compile(r"mongodb(?:\+srv)?://\S+")
_PASSWORD_RE = re.compile(r"(?i)(password[\"']?\s*[:=]\s*)(\S+)")
_LINK_ERRORS = (PyMongoError, BotoCoreError, ClientError, KeyError, TypeError, ValueError, OSError)

_MIN_HOST_LABELS = 2

_secrets = boto3.client("secretsmanager")


class _ClientCache:
    client: MongoClient | None = None


def handler(_event: object, _context: object) -> dict[str, object]:
    try:
        return _response(_prove())
    except OperationFailure as exc:
        if exc.code != _AUTHENTICATION_FAILED:
            return _error(exc)
    except _LINK_ERRORS as exc:
        return _error(exc)
    # Rotation invalidates a cached password. The marker write is one fixed id,
    # so a single retry replaces that document instead of appending another.
    _close_client()
    try:
        return _response(_prove())
    except _LINK_ERRORS as exc:
        return _error(exc)


def connect() -> MongoClient:
    if _ClientCache.client is not None:
        return _ClientCache.client
    secret = _secrets.get_secret_value(SecretId=Resources.docdb.secret_arn)
    raw = secret.get("SecretString")
    if not isinstance(raw, str):
        raise TypeError("Secrets Manager secret has no SecretString")
    password = json.loads(raw)["password"]
    if not isinstance(password, str):
        raise TypeError("DocumentDB secret password is not a string")
    client = MongoClient(
        Resources.docdb.connection_uri,
        username=Resources.docdb.username,
        password=password,
        tls=True,
        tlsCAFile=Resources.docdb.ca_file,
        serverSelectionTimeoutMS=_SELECTION_TIMEOUT_MS,
    )
    del password
    _ClientCache.client = client
    return client


def _prove() -> dict[str, object]:
    client = connect()
    ping = client.admin.command("ping")
    expected = Resources.docdb.host
    seen = _seen_hosts(client)
    host_matches = _host_matches(expected, seen)
    collection = client[_DATABASE][_COLLECTION]
    collection.replace_one(
        {"_id": _DOC_ID},
        {"_id": _DOC_ID, "marker": _MARKER},
        upsert=True,
    )
    found = collection.find_one({"_id": _DOC_ID})
    read_back = isinstance(found, dict) and found.get("marker") == _MARKER
    ping_ok = float(ping.get("ok", 0)) == _PING_OK
    return {
        "ok": bool(ping_ok and read_back and host_matches),
        "ping": ping_ok,
        "read_back": read_back,
        "host_matches": host_matches,
        "expected_host": expected,
        "seen_hosts": seen,
    }


def _seen_hosts(client: MongoClient) -> list[str]:
    hosts = {host for host, _port in client.nodes}
    try:
        address = client.address
    except InvalidOperation:
        address = None
    if address is not None:
        hosts.add(address[0])
    return sorted(hosts)


def _cluster_token(host: str) -> str | None:
    """Shared DocumentDB cluster id from a cluster, reader, or instance hostname."""
    if not host.endswith(_DOCDB_SUFFIX):
        return None
    labels = host[: -len(_DOCDB_SUFFIX)].split(".")
    if len(labels) < _MIN_HOST_LABELS:
        return None
    token = labels[-2]
    for prefix in ("cluster-ro-", "cluster-"):
        if token.startswith(prefix):
            return token.removeprefix(prefix)
    return token


def _host_matches(expected: str, seen: list[str]) -> bool:
    if not expected or not seen:
        return False
    if expected in _LOCAL_HOSTS or any(host in _LOCAL_HOSTS for host in seen):
        return False
    if expected in seen:
        return True
    expected_token = _cluster_token(expected)
    if not expected_token:
        return False
    return any(_cluster_token(host) == expected_token for host in seen)


def _close_client() -> None:
    if _ClientCache.client is not None:
        _ClientCache.client.close()
    _ClientCache.client = None


def _response(body: dict[str, object]) -> dict[str, object]:
    return {
        "statusCode": _HTTP_OK,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _error(exc: BaseException) -> dict[str, object]:
    text = _URI_RE.sub("[uri]", str(exc))
    text = _PASSWORD_RE.sub(r"\1[redacted]", text)
    payload = {"ok": False, "error": f"{type(exc).__name__}: {text}"[:_ERROR_LIMIT]}
    return {
        "statusCode": _HTTP_ERROR,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }
