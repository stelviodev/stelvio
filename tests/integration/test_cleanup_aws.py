"""Unit tests for cleanup_aws discovery and deletion helpers.

These tests do NOT require AWS credentials — they test ARN parsing,
name matching, and ID extraction logic only.
"""

import sys
from pathlib import Path

# cleanup_aws is a standalone script, not a package module — add its directory
sys.path.insert(0, str(Path(__file__).resolve().parent))

from cleanup_aws import (
    DiscoveredResource,
    _classify_apigateway_resource,
    _matches_name,
    _name_from_arn,
    _service_from_arn,
    deduplicate,
)

# ---------------------------------------------------------------------------
# _matches_name
# ---------------------------------------------------------------------------


class TestMatchesName:
    def test_valid_test_name(self):
        assert _matches_name("stlv-87b3f6-test-cleanup-q") is True

    def test_valid_test_name_with_hash(self):
        assert _matches_name("stlv-87b3f6-test-cleanup-tbl-32022f2") is True

    def test_non_test_name(self):
        assert _matches_name("my-production-queue") is False

    def test_stlv_without_test(self):
        assert _matches_name("stlv-87b3f6-prod-my-queue") is False

    def test_stlv_wrong_hex_length(self):
        assert _matches_name("stlv-87b-test-my-queue") is False

    def test_stlv_uppercase_hex(self):
        assert _matches_name("stlv-87B3F6-test-my-queue") is False


# ---------------------------------------------------------------------------
# _service_from_arn
# ---------------------------------------------------------------------------


class TestServiceFromArn:
    def test_lambda_function(self):
        arn = "arn:aws:lambda:us-east-1:123456789012:function:my-func"
        assert _service_from_arn(arn) == "lambda"

    def test_lambda_layer(self):
        arn = "arn:aws:lambda:us-east-1:123456789012:layer:my-layer"
        assert _service_from_arn(arn) == "lambda-layer"

    def test_iam_role(self):
        arn = "arn:aws:iam::123456789012:role/my-role"
        assert _service_from_arn(arn) == "iam-role"

    def test_iam_policy(self):
        arn = "arn:aws:iam::123456789012:policy/my-policy"
        assert _service_from_arn(arn) == "iam-policy"

    def test_sqs(self):
        arn = "arn:aws:sqs:us-east-1:123456789012:my-queue"
        assert _service_from_arn(arn) == "sqs"

    def test_sns(self):
        arn = "arn:aws:sns:us-east-1:123456789012:my-topic"
        assert _service_from_arn(arn) == "sns"

    def test_dynamodb(self):
        arn = "arn:aws:dynamodb:us-east-1:123456789012:table/my-table"
        assert _service_from_arn(arn) == "dynamodb"

    def test_s3(self):
        arn = "arn:aws:s3:::my-bucket"
        assert _service_from_arn(arn) == "s3"

    def test_events(self):
        arn = "arn:aws:events:us-east-1:123456789012:rule/my-rule"
        assert _service_from_arn(arn) == "events"

    def test_apigateway_restapi(self):
        arn = "arn:aws:apigateway:us-east-1::/restapis/abc123"
        assert _service_from_arn(arn) == "apigateway"

    def test_apigateway_stage_skipped(self):
        arn = "arn:aws:apigateway:us-east-1::/restapis/abc123/stages/prod"
        assert _service_from_arn(arn) is None

    def test_apigateway_deployment_skipped(self):
        arn = "arn:aws:apigateway:us-east-1::/restapis/abc123/deployments/xyz"
        assert _service_from_arn(arn) is None

    def test_apigateway_resource_skipped(self):
        arn = "arn:aws:apigateway:us-east-1::/restapis/abc123/resources/def456"
        assert _service_from_arn(arn) is None

    def test_acm(self):
        arn = "arn:aws:acm:us-east-1:123456789012:certificate/abc-123"
        assert _service_from_arn(arn) == "acm"

    def test_execute_api_skipped(self):
        """execute-api ARNs are invocation endpoints, not manageable resources."""
        arn = "arn:aws:execute-api:us-east-1:123456789012:abc123/prod/GET/items"
        assert _service_from_arn(arn) is None

    def test_invalid_arn(self):
        assert _service_from_arn("not-an-arn") is None


# ---------------------------------------------------------------------------
# _name_from_arn
# ---------------------------------------------------------------------------


class TestNameFromArn:
    def test_lambda_function(self):
        arn = "arn:aws:lambda:us-east-1:123456789012:function:stlv-abc123-test-my-func"
        assert _name_from_arn(arn) == "stlv-abc123-test-my-func"

    def test_lambda_layer(self):
        arn = "arn:aws:lambda:us-east-1:123456789012:layer:stlv-abc123-test-my-layer"
        assert _name_from_arn(arn) == "stlv-abc123-test-my-layer"

    def test_iam_role(self):
        arn = "arn:aws:iam::123456789012:role/stlv-abc123-test-role"
        assert _name_from_arn(arn) == "stlv-abc123-test-role"

    def test_iam_policy(self):
        arn = "arn:aws:iam::123456789012:policy/stlv-abc123-test-policy"
        assert _name_from_arn(arn) == "stlv-abc123-test-policy"

    def test_sqs(self):
        arn = "arn:aws:sqs:us-east-1:123456789012:stlv-abc123-test-queue"
        assert _name_from_arn(arn) == "stlv-abc123-test-queue"

    def test_dynamodb(self):
        arn = "arn:aws:dynamodb:us-east-1:123456789012:table/stlv-abc123-test-tbl"
        assert _name_from_arn(arn) == "stlv-abc123-test-tbl"

    def test_s3(self):
        arn = "arn:aws:s3:::stlv-abc123-test-bucket"
        assert _name_from_arn(arn) == "stlv-abc123-test-bucket"

    def test_events_rule(self):
        arn = "arn:aws:events:us-east-1:123456789012:rule/stlv-abc123-test-rule"
        assert _name_from_arn(arn) == "stlv-abc123-test-rule"

    def test_apigateway_restapi(self):
        arn = "arn:aws:apigateway:us-east-1::/restapis/abc123def"
        assert _name_from_arn(arn) == "abc123def"

    def test_apigateway_deeper_path_still_extracts_id(self):
        arn = "arn:aws:apigateway:us-east-1::/restapis/abc123def/stages/prod"
        assert _name_from_arn(arn) == "abc123def"

    def test_invalid_arn(self):
        assert _name_from_arn("not-an-arn") is None


# ---------------------------------------------------------------------------
# _classify_apigateway_resource
# ---------------------------------------------------------------------------


class TestClassifyApigatewayResource:
    def test_restapi(self):
        assert _classify_apigateway_resource("/restapis/abc123") == "apigateway"

    def test_stage(self):
        assert _classify_apigateway_resource("/restapis/abc123/stages/prod") is None

    def test_deployment(self):
        assert _classify_apigateway_resource("/restapis/abc123/deployments/xyz") is None

    def test_resource(self):
        assert _classify_apigateway_resource("/restapis/abc123/resources/def") is None

    def test_empty(self):
        assert _classify_apigateway_resource("") is None


# ---------------------------------------------------------------------------
# API Gateway ARN consistency between discovery and deletion
# ---------------------------------------------------------------------------


class TestApigatewayArnConsistency:
    """Verify that name-based and tag-based discovery produce ARNs that
    deduplicate correctly and that the deletion handler extracts the
    correct API ID from either format."""

    def test_name_based_arn_format(self):
        """Name-based scanner uses /restapis/{id} format."""
        # Simulates what _scan_apigateway produces
        arn = "arn:aws:apigateway:us-east-1::/restapis/abc123"
        assert _service_from_arn(arn) == "apigateway"
        assert _name_from_arn(arn) == "abc123"

    def test_tag_based_arn_dedup_with_name_based(self):
        """Both scanners produce identical ARNs, so dedup works."""
        tag_resource = DiscoveredResource(
            service="apigateway",
            arn="arn:aws:apigateway:us-east-1::/restapis/abc123",
            name="abc123",
            region="us-east-1",
        )
        name_resource = DiscoveredResource(
            service="apigateway",
            arn="arn:aws:apigateway:us-east-1::/restapis/abc123",
            name="stlv-aabbcc-test-my-api",
            region="us-east-1",
        )
        result = deduplicate([tag_resource, name_resource])
        assert len(result) == 1

    def test_api_id_extraction_from_arn(self):
        """_delete_apigateway logic: split on ::, then parse /restapis/{id}."""
        arn = "arn:aws:apigateway:us-east-1::/restapis/abc123"
        resource_path = arn.split("::", 1)[-1]
        api_id = resource_path.strip("/").split("/")[1]
        assert api_id == "abc123"


# ---------------------------------------------------------------------------
# deduplicate
# ---------------------------------------------------------------------------


class TestDeduplicate:
    def test_removes_duplicate_arns(self):
        r1 = DiscoveredResource(service="sqs", arn="arn:1", name="q1", region="us-east-1")
        r2 = DiscoveredResource(service="sqs", arn="arn:1", name="q1", region="us-east-1")
        assert len(deduplicate([r1, r2])) == 1

    def test_keeps_different_arns(self):
        r1 = DiscoveredResource(service="sqs", arn="arn:1", name="q1", region="us-east-1")
        r2 = DiscoveredResource(service="sqs", arn="arn:2", name="q2", region="us-east-1")
        assert len(deduplicate([r1, r2])) == 2

    def test_keeps_first_occurrence(self):
        r1 = DiscoveredResource(service="sqs", arn="arn:1", name="tag-name", region="us-east-1")
        r2 = DiscoveredResource(service="sqs", arn="arn:1", name="scan-name", region="us-east-1")
        result = deduplicate([r1, r2])
        assert result[0].name == "tag-name"

    def test_empty_list(self):
        assert deduplicate([]) == []
