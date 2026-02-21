"""Scenario tests: API Gateway end-to-end.

Verifies that API Gateway routes actually handle HTTP requests correctly —
CRUD handler reads/writes DynamoDB, authorizers accept/reject tokens, and
the async API pattern (API → Queue → Worker) processes jobs end-to-end.
"""

import json
import time

import pytest

from stelvio.aws.api_gateway import Api
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function
from stelvio.aws.queue import Queue

from .assert_helpers import http_request, poll_dynamo_items

pytestmark = pytest.mark.integration

# API Gateway needs a brief delay after deploy for deployment to propagate
_API_DEPLOY_WAIT = 3


def test_scenario_api_crud(stelvio_env, project_dir):
    """API CRUD: POST → GET → DELETE → GET 404 via DynamoDB-backed handler."""

    def infra():
        items = DynamoTable("items", fields={"pk": "S"}, partition_key="pk")
        fn = Function("api-handler", handler="handlers/api_crud.main", links=[items])
        api = Api("crud")
        api.route("POST", "/items", fn)
        api.route("GET", "/items/{id}", fn)
        api.route("DELETE", "/items/{id}", fn)

    outputs = stelvio_env.deploy(infra)
    base_url = outputs["api_crud_invoke_url"]

    # Wait for API Gateway deployment to stabilize
    time.sleep(_API_DEPLOY_WAIT)

    # Create item
    status, body = http_request(
        f"{base_url}/items", "POST", body={"pk": "item-1", "name": "Widget"}
    )
    assert status == 201

    # Read item back
    status, body = http_request(f"{base_url}/items/item-1")
    assert status == 200
    item = json.loads(body)
    assert item["pk"] == "item-1"
    assert item["name"] == "Widget"

    # Delete item
    status, _ = http_request(f"{base_url}/items/item-1", "DELETE")
    assert status == 200

    # Verify deletion
    status, _ = http_request(f"{base_url}/items/item-1")
    assert status == 404


def test_scenario_api_auth(stelvio_env, project_dir):
    """Authorizer rejects invalid tokens and allows valid ones."""

    def infra():
        api = Api("auth")
        auth = api.add_token_authorizer("jwt", "handlers/auth.handler")
        api.route("GET", "/secure", "handlers/echo.main", auth=auth)

    outputs = stelvio_env.deploy(infra)
    url = outputs["api_auth_invoke_url"].rstrip("/") + "/secure"
    time.sleep(_API_DEPLOY_WAIT)

    # No token → 401
    status, _ = http_request(url)
    assert status == 401

    # Bad token → 403
    status, _ = http_request(url, headers={"Authorization": "Bearer deny"})
    assert status == 403

    # Valid token → 200
    status, body = http_request(url, headers={"Authorization": "Bearer allow"})
    assert status == 200
    event = json.loads(body)
    assert event["httpMethod"] == "GET"


# --- Composite: async API pattern ---


def test_scenario_api_to_queue_to_worker(stelvio_env, project_dir):
    """POST to API → Lambda queues message → Worker writes to DynamoDB."""

    def infra():
        results = DynamoTable("results", fields={"pk": "S"}, partition_key="pk")
        jobs = Queue("jobs")

        # API handler sends request body to queue
        submitter = Function(
            "submitter",
            handler="handlers/queue_sender.main",
            links=[jobs],
        )

        # Worker processes queue messages, writes to results table
        jobs.subscribe("processor", "handlers/event_recorder.main", links=[results])

        api = Api("async")
        api.route("POST", "/submit", submitter)

    outputs = stelvio_env.deploy(infra)
    base_url = outputs["api_async_invoke_url"]
    time.sleep(_API_DEPLOY_WAIT)

    # Submit a job via API
    status, body = http_request(
        f"{base_url}/submit", "POST", body={"job": "resize-image", "id": "job-42"}
    )
    assert status == 200
    resp = json.loads(body)
    assert "messageId" in resp

    # Poll: worker should process the queued message and write to results
    items = poll_dynamo_items(outputs["dynamotable_results_name"])
    assert len(items) >= 1
    event = json.loads(items[0]["event"])
    # Worker received SQS event; body contains our original job payload
    sqs_body = json.loads(event["Records"][0]["body"])
    assert sqs_body["job"] == "resize-image"
    assert sqs_body["id"] == "job-42"
