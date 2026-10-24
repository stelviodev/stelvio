"""Local dev handler: ordinary HTTPS to a private API Gateway/Lambda target."""

import json
import os
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

_invocations = os.environ.get("STLV_TEST_TUNNEL_INVOCATIONS")
if _invocations:
    with Path(_invocations).open("a") as stream:
        stream.write(
            json.dumps({"pid": os.getpid(), "target": os.environ["TUNNEL_VPC"], "kind": "import"})
            + "\n"
        )


def handler(event, _context):
    nonce = (event.get("queryStringParameters") or {}).get("marker")
    if _invocations:
        with Path(_invocations).open("a") as stream:
            stream.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "target": os.environ["TUNNEL_VPC"],
                        "kind": "invoke",
                        "nonce": nonce,
                    }
                )
                + "\n"
            )
    if "PRIVATE_API_URL" not in os.environ:
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "plain": True,
                    "app": os.environ["PROOF_APP"],
                    "nonce": nonce,
                    "pid": os.getpid(),
                    "local": bool(_invocations),
                }
            ),
        }
    request = Request(
        os.environ["PRIVATE_API_URL"],
        json.dumps({"nonce": nonce, "payload": "dev-private-payload"}).encode(),
        {"Content-Type": "application/json", "Connection": "close"},
        method="POST",
    )
    with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
        echo = json.loads(response.read(65537))
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"echo": echo, "pid": os.getpid(), "local": bool(_invocations)}),
    }
