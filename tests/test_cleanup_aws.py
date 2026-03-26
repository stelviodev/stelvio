"""Unit tests for cleanup_aws discovery and deletion helpers.

These tests do NOT require AWS credentials — they test ARN parsing,
name matching, and ID extraction logic only.
"""

from tests.integration.cleanup_aws import (
    DiscoveredResource,
    _classify_apigateway_resource,
    _delete_cloudfront_distribution,
    _delete_resource,
    _delete_route53_record,
    _is_test_app_tag,
    _is_test_route53_record_name,
    _matches_name,
    _name_from_arn,
    _parse_route53_record_key,
    _route53_record_key,
    _scan_route53_records,
    _service_from_arn,
    deduplicate,
    discover_by_tags,
)

# ---------------------------------------------------------------------------
# _matches_name
# ---------------------------------------------------------------------------


class TestMatchesName:
    def test_valid_test_name(self):
        assert _matches_name("stelvio-87b3f6-test-cleanup-q") is True

    def test_valid_test_name_with_hash(self):
        assert _matches_name("stelvio-87b3f6-test-cleanup-tbl-32022f2") is True

    def test_non_test_name(self):
        assert _matches_name("my-production-queue") is False

    def test_stelvio_without_test(self):
        assert _matches_name("stelvio-87b3f6-prod-my-queue") is False

    def test_stelvio_wrong_hex_length(self):
        assert _matches_name("stelvio-87b-test-my-queue") is False

    def test_stelvio_uppercase_hex(self):
        assert _matches_name("stelvio-87B3F6-test-my-queue") is False


class TestAppTagMatching:
    def test_exact_test_app_tag(self):
        assert _is_test_app_tag("stelvio-a1b2c3") is True

    def test_prefix_only_is_not_enough(self):
        assert _is_test_app_tag("stelvio-production") is False

    def test_test_suffix_is_not_valid_for_app_tag(self):
        assert _is_test_app_tag("stelvio-a1b2c3-test-app") is False

    def test_uppercase_hex_is_not_valid(self):
        assert _is_test_app_tag("stelvio-A1B2C3") is False


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
        arn = "arn:aws:lambda:us-east-1:123456789012:function:stelvio-abc123-test-my-func"
        assert _name_from_arn(arn) == "stelvio-abc123-test-my-func"

    def test_lambda_layer(self):
        arn = "arn:aws:lambda:us-east-1:123456789012:layer:stelvio-abc123-test-my-layer"
        assert _name_from_arn(arn) == "stelvio-abc123-test-my-layer"

    def test_iam_role(self):
        arn = "arn:aws:iam::123456789012:role/stelvio-abc123-test-role"
        assert _name_from_arn(arn) == "stelvio-abc123-test-role"

    def test_iam_policy(self):
        arn = "arn:aws:iam::123456789012:policy/stelvio-abc123-test-policy"
        assert _name_from_arn(arn) == "stelvio-abc123-test-policy"

    def test_sqs(self):
        arn = "arn:aws:sqs:us-east-1:123456789012:stelvio-abc123-test-queue"
        assert _name_from_arn(arn) == "stelvio-abc123-test-queue"

    def test_dynamodb(self):
        arn = "arn:aws:dynamodb:us-east-1:123456789012:table/stelvio-abc123-test-tbl"
        assert _name_from_arn(arn) == "stelvio-abc123-test-tbl"

    def test_s3(self):
        arn = "arn:aws:s3:::stelvio-abc123-test-bucket"
        assert _name_from_arn(arn) == "stelvio-abc123-test-bucket"

    def test_events_rule(self):
        arn = "arn:aws:events:us-east-1:123456789012:rule/stelvio-abc123-test-rule"
        assert _name_from_arn(arn) == "stelvio-abc123-test-rule"

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
            name="stelvio-aabbcc-test-my-api",
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


class TestRoute53Helpers:
    def test_is_test_route53_record_name_direct_label(self):
        assert _is_test_route53_record_name("stelvio-87b3f6-test-api.example.com.") is True

    def test_is_test_route53_record_name_nested_label(self):
        assert _is_test_route53_record_name("_abc.stelvio-87b3f6-test-api.example.com.") is True

    def test_is_test_route53_record_name_non_test(self):
        assert _is_test_route53_record_name("api.example.com.") is False

    def test_is_test_route53_record_name_requires_suffix_after_test(self):
        assert _is_test_route53_record_name("stelvio-a1b2c3-test") is False

    def test_route53_record_key_round_trip_with_set_identifier(self):
        key = _route53_record_key(
            zone_id="Z123",
            record_type="A",
            record_name="stelvio-87b3f6-test-api.example.com.",
            set_identifier="blue",
        )
        assert _parse_route53_record_key(key) == (
            "Z123",
            "A",
            "stelvio-87b3f6-test-api.example.com.",
            "blue",
        )

    def test_parse_route53_record_key_legacy_format(self):
        assert _parse_route53_record_key("Z123::A::stelvio-87b3f6-test-api.example.com.") == (
            "Z123",
            "A",
            "stelvio-87b3f6-test-api.example.com.",
            None,
        )


class _FakePaginator:
    def __init__(self, pages: list[dict]) -> None:
        self._pages = pages
        self.calls: list[dict] = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return self._pages


class _FakeRoute53Client:
    def __init__(self, pages: list[dict]) -> None:
        self._paginator = _FakePaginator(pages)
        self.deletions: list[dict] = []

    def get_paginator(self, name: str) -> _FakePaginator:
        assert name == "list_resource_record_sets"
        return self._paginator

    def change_resource_record_sets(self, HostedZoneId: str, ChangeBatch: dict) -> None:  # noqa: N803
        self.deletions.append({"HostedZoneId": HostedZoneId, "ChangeBatch": ChangeBatch})


class _FakeCloudFrontWaiter:
    def __init__(self) -> None:
        self.wait_calls: list[dict] = []

    def wait(self, **kwargs) -> None:
        self.wait_calls.append(kwargs)


class _FakeCloudFrontClient:
    def __init__(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.waiter = _FakeCloudFrontWaiter()
        self.updated = False
        self.deleted: tuple[str, str] | None = None

    def get_distribution_config(self, Id: str) -> dict:  # noqa: N803
        return {"DistributionConfig": {"Enabled": self.enabled}, "ETag": "etag-initial"}

    def update_distribution(self, Id: str, DistributionConfig: dict, IfMatch: str) -> dict:  # noqa: N803
        assert Id
        assert DistributionConfig["Enabled"] is False
        assert IfMatch
        self.updated = True
        self.enabled = False
        return {"ETag": "etag-updated"}

    def get_waiter(self, name: str) -> _FakeCloudFrontWaiter:
        assert name == "distribution_deployed"
        return self.waiter

    def get_distribution(self, Id: str) -> dict:  # noqa: N803
        assert Id
        return {"ETag": "etag-final"}

    def delete_distribution(self, Id: str, IfMatch: str) -> None:  # noqa: N803
        self.deleted = (Id, IfMatch)


class _FakeSession:
    def __init__(
        self, *, route53_client: _FakeRoute53Client | None = None, cloudfront_client=None
    ):
        self._route53_client = route53_client
        self._cloudfront_client = cloudfront_client

    def client(self, service: str):
        if service == "route53":
            assert self._route53_client is not None
            return self._route53_client
        if service == "cloudfront":
            assert self._cloudfront_client is not None
            return self._cloudfront_client
        raise AssertionError(f"Unexpected service: {service}")


class TestRoute53ScanAndDelete:
    def test_scan_route53_records_filters_to_test_names(self, monkeypatch):
        monkeypatch.setenv("STELVIO_TEST_DNS_ZONE_ID", "ZTEST")
        pages = [
            {
                "ResourceRecordSets": [
                    {"Name": "example.com.", "Type": "NS"},
                    {"Name": "example.com.", "Type": "SOA"},
                    {
                        "Name": "stelvio-87b3f6-test-api.example.com.",
                        "Type": "A",
                        "TTL": 60,
                        "ResourceRecords": [{"Value": "1.1.1.1"}],
                    },
                    {
                        "Name": "_hash.stelvio-87b3f6-test-api.example.com.",
                        "Type": "CNAME",
                        "TTL": 60,
                        "ResourceRecords": [{"Value": "abc.cloudfront.net"}],
                    },
                    {
                        "Name": "prod-api.example.com.",
                        "Type": "A",
                        "TTL": 60,
                        "ResourceRecords": [{"Value": "2.2.2.2"}],
                    },
                ]
            }
        ]
        client = _FakeRoute53Client(pages)
        session = _FakeSession(route53_client=client)

        results: list[DiscoveredResource] = []
        _scan_route53_records(session, results)

        assert len(results) == 2
        assert all(r.service == "route53" for r in results)
        assert results[0].arn == "ZTEST::A::stelvio-87b3f6-test-api.example.com.::-"
        assert results[1].arn == "ZTEST::CNAME::_hash.stelvio-87b3f6-test-api.example.com.::-"

    def test_delete_route53_record_matches_set_identifier(self):
        pages = [
            {
                "ResourceRecordSets": [
                    {
                        "Name": "stelvio-87b3f6-test-api.example.com.",
                        "Type": "A",
                        "SetIdentifier": "blue",
                        "TTL": 60,
                        "ResourceRecords": [{"Value": "1.1.1.1"}],
                    },
                    {
                        "Name": "stelvio-87b3f6-test-api.example.com.",
                        "Type": "A",
                        "SetIdentifier": "green",
                        "TTL": 60,
                        "ResourceRecords": [{"Value": "2.2.2.2"}],
                    },
                ]
            }
        ]
        client = _FakeRoute53Client(pages)
        session = _FakeSession(route53_client=client)
        resource = DiscoveredResource(
            service="route53",
            arn=_route53_record_key(
                zone_id="ZTEST",
                record_type="A",
                record_name="stelvio-87b3f6-test-api.example.com.",
                set_identifier="green",
            ),
            name="stelvio-87b3f6-test-api.example.com. [green]",
            region="global",
        )

        _delete_route53_record(session, resource)

        assert len(client.deletions) == 1
        deleted_rrs = client.deletions[0]["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
        assert deleted_rrs["SetIdentifier"] == "green"

    def test_delete_resource_treats_missing_route53_record_as_already_gone(self):
        client = _FakeRoute53Client([{"ResourceRecordSets": []}])
        session = _FakeSession(route53_client=client)
        resource = DiscoveredResource(
            service="route53",
            arn=_route53_record_key(
                zone_id="ZTEST",
                record_type="A",
                record_name="stelvio-87b3f6-test-api.example.com.",
            ),
            name="stelvio-87b3f6-test-api.example.com.",
            region="global",
        )

        _delete_resource(session, resource)
        assert client.deletions == []


class TestCloudFrontDelete:
    def test_delete_cloudfront_distribution_waits_even_if_already_disabled(self):
        cf_client = _FakeCloudFrontClient(enabled=False)
        session = _FakeSession(cloudfront_client=cf_client)
        resource = DiscoveredResource(
            service="cloudfront",
            arn="arn:aws:cloudfront::123456789012:distribution/E123ABC",
            name="E123ABC",
            region="global",
        )

        _delete_cloudfront_distribution(session, resource)

        assert cf_client.updated is False
        assert cf_client.waiter.wait_calls == [{"Id": "E123ABC"}]
        assert cf_client.deleted == ("E123ABC", "etag-final")


class _FakeTaggingPaginator:
    def __init__(self, pages: list[dict]) -> None:
        self.pages = pages
        self.paginate_calls: list[dict] = []

    def paginate(self, **kwargs):
        self.paginate_calls.append(kwargs)
        return self.pages


class _FakeTaggingClient:
    def __init__(self, pages: list[dict]) -> None:
        self.paginator = _FakeTaggingPaginator(pages)

    def get_paginator(self, name: str) -> _FakeTaggingPaginator:
        assert name == "get_resources"
        return self.paginator


class _FakeTaggingSession:
    def __init__(self, client: _FakeTaggingClient) -> None:
        self._client = client

    def client(self, service: str) -> _FakeTaggingClient:
        assert service == "resourcegroupstaggingapi"
        return self._client


class TestDiscoverByTags:
    def test_discover_by_tags_keeps_only_stelvio_app_tag(self, monkeypatch):
        pages = [
            {
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": (
                            "arn:aws:lambda:us-east-1:123456789012:function:stelvio-a1b2c3-test-fn"
                        ),
                        "Tags": [
                            {"Key": "stelvio:env", "Value": "test"},
                            {"Key": "stelvio:app", "Value": "stelvio-a1b2c3"},
                        ],
                    },
                    {
                        "ResourceARN": "arn:aws:lambda:us-east-1:123456789012:function:prod-fn",
                        "Tags": [
                            {"Key": "stelvio:env", "Value": "test"},
                            {"Key": "stelvio:app", "Value": "prod-app"},
                        ],
                    },
                    {
                        "ResourceARN": "arn:aws:lambda:us-east-1:123456789012:function:no-app-tag",
                        "Tags": [{"Key": "stelvio:env", "Value": "test"}],
                    },
                ]
            }
        ]
        fake_client = _FakeTaggingClient(pages)

        def _fake_create_session(profile: str | None, region: str) -> _FakeTaggingSession:
            assert profile is None
            assert region == "us-east-1"
            return _FakeTaggingSession(fake_client)

        monkeypatch.setattr("tests.integration.cleanup_aws._create_session", _fake_create_session)

        result = discover_by_tags(profile=None, regions=["us-east-1"])

        assert len(result) == 1
        assert result[0] == DiscoveredResource(
            service="lambda",
            arn="arn:aws:lambda:us-east-1:123456789012:function:stelvio-a1b2c3-test-fn",
            name="stelvio-a1b2c3-test-fn",
            region="us-east-1",
        )
        assert fake_client.paginator.paginate_calls == [
            {"TagFilters": [{"Key": "stelvio:env", "Values": ["test"]}]}
        ]

    def test_discover_by_tags_skips_non_exact_stelvio_prefix(self, monkeypatch):
        pages = [
            {
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": "arn:aws:lambda:us-east-1:123456789012:function:prod-fn",
                        "Tags": [
                            {"Key": "stelvio:env", "Value": "test"},
                            {"Key": "stelvio:app", "Value": "stelvio-production"},
                        ],
                    }
                ]
            }
        ]
        fake_client = _FakeTaggingClient(pages)

        def _fake_create_session(profile: str | None, region: str) -> _FakeTaggingSession:
            assert profile is None
            assert region == "us-east-1"
            return _FakeTaggingSession(fake_client)

        monkeypatch.setattr("tests.integration.cleanup_aws._create_session", _fake_create_session)

        assert discover_by_tags(profile=None, regions=["us-east-1"]) == []

    def test_discover_by_tags_skips_unrecognized_resource_types(self, monkeypatch):
        pages = [
            {
                "ResourceTagMappingList": [
                    {
                        "ResourceARN": (
                            "arn:aws:execute-api:us-east-1:123456789012:abc123/prod/GET/items"
                        ),
                        "Tags": [
                            {"Key": "stelvio:env", "Value": "test"},
                            {"Key": "stelvio:app", "Value": "stelvio-a1b2c3"},
                        ],
                    }
                ]
            }
        ]
        fake_client = _FakeTaggingClient(pages)

        def _fake_create_session(profile: str | None, region: str) -> _FakeTaggingSession:
            assert profile is None
            assert region == "us-east-1"
            return _FakeTaggingSession(fake_client)

        monkeypatch.setattr("tests.integration.cleanup_aws._create_session", _fake_create_session)

        assert discover_by_tags(profile=None, regions=["us-east-1"]) == []
