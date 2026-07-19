"""Unit tests for cleanup_aws discovery and deletion helpers.

These tests do NOT require AWS credentials — they test ARN parsing,
name matching, and ID extraction logic only.
"""

from pytest import mark, param

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


@mark.parametrize(
    ("name", "expected"),
    [
        param("stlv-87b3f6-test-cleanup-q", True, id="test-name"),
        param("stlv-87b3f6-test-cleanup-tbl-32022f2", True, id="test-name-with-hash"),
        param("my-production-queue", False, id="non-test-name"),
        param("stlv-87b3f6-prod-my-queue", False, id="stlv-without-test"),
        param("stlv-87b-test-my-queue", False, id="wrong-hex-length"),
        param("stlv-87B3F6-test-my-queue", False, id="uppercase-hex"),
    ],
)
def test_matches_name(name, expected):
    assert _matches_name(name) is expected


@mark.parametrize(
    ("tag", "expected"),
    [
        param("stlv-a1b2c3", True, id="exact-test-app-tag"),
        param("stlv-production", False, id="prefix-only-not-enough"),
        param("stlv-a1b2c3-test-app", False, id="test-suffix-not-valid"),
        param("stlv-A1B2C3", False, id="uppercase-hex-not-valid"),
    ],
)
def test_is_test_app_tag(tag, expected):
    assert _is_test_app_tag(tag) is expected


# ---------------------------------------------------------------------------
# _service_from_arn
# ---------------------------------------------------------------------------


@mark.parametrize(
    ("arn", "expected"),
    [
        param("arn:aws:lambda:us-east-1:123456789012:function:my-func", "lambda", id="lambda"),
        param(
            "arn:aws:lambda:us-east-1:123456789012:layer:my-layer",
            "lambda-layer",
            id="lambda-layer",
        ),
        param("arn:aws:iam::123456789012:role/my-role", "iam-role", id="iam-role"),
        param("arn:aws:iam::123456789012:policy/my-policy", "iam-policy", id="iam-policy"),
        param("arn:aws:sqs:us-east-1:123456789012:my-queue", "sqs", id="sqs"),
        param("arn:aws:sns:us-east-1:123456789012:my-topic", "sns", id="sns"),
        param("arn:aws:dynamodb:us-east-1:123456789012:table/my-table", "dynamodb", id="dynamodb"),
        param("arn:aws:s3:::my-bucket", "s3", id="s3"),
        param("arn:aws:events:us-east-1:123456789012:rule/my-rule", "events", id="events"),
        param(
            "arn:aws:apigateway:us-east-1::/restapis/abc123", "apigateway", id="apigateway-restapi"
        ),
        param(
            "arn:aws:apigateway:us-east-1::/restapis/abc123/stages/prod",
            None,
            id="apigateway-stage-skipped",
        ),
        param(
            "arn:aws:apigateway:us-east-1::/restapis/abc123/deployments/xyz",
            None,
            id="apigateway-deployment-skipped",
        ),
        param(
            "arn:aws:apigateway:us-east-1::/restapis/abc123/resources/def456",
            None,
            id="apigateway-resource-skipped",
        ),
        param("arn:aws:acm:us-east-1:123456789012:certificate/abc-123", "acm", id="acm"),
        # execute-api ARNs are invocation endpoints, not manageable resources
        param(
            "arn:aws:execute-api:us-east-1:123456789012:abc123/prod/GET/items",
            None,
            id="execute-api-skipped",
        ),
        param("not-an-arn", None, id="invalid-arn"),
    ],
)
def test_service_from_arn(arn, expected):
    assert _service_from_arn(arn) == expected


# ---------------------------------------------------------------------------
# _name_from_arn
# ---------------------------------------------------------------------------


@mark.parametrize(
    ("arn", "expected"),
    [
        param(
            "arn:aws:lambda:us-east-1:123456789012:function:stlv-abc123-test-my-func",
            "stlv-abc123-test-my-func",
            id="lambda",
        ),
        param(
            "arn:aws:lambda:us-east-1:123456789012:layer:stlv-abc123-test-my-layer",
            "stlv-abc123-test-my-layer",
            id="lambda-layer",
        ),
        param(
            "arn:aws:iam::123456789012:role/stlv-abc123-test-role",
            "stlv-abc123-test-role",
            id="iam-role",
        ),
        param(
            "arn:aws:iam::123456789012:policy/stlv-abc123-test-policy",
            "stlv-abc123-test-policy",
            id="iam-policy",
        ),
        param(
            "arn:aws:sqs:us-east-1:123456789012:stlv-abc123-test-queue",
            "stlv-abc123-test-queue",
            id="sqs",
        ),
        param(
            "arn:aws:dynamodb:us-east-1:123456789012:table/stlv-abc123-test-tbl",
            "stlv-abc123-test-tbl",
            id="dynamodb",
        ),
        param("arn:aws:s3:::stlv-abc123-test-bucket", "stlv-abc123-test-bucket", id="s3"),
        param(
            "arn:aws:events:us-east-1:123456789012:rule/stlv-abc123-test-rule",
            "stlv-abc123-test-rule",
            id="events-rule",
        ),
        param("arn:aws:apigateway:us-east-1::/restapis/abc123def", "abc123def", id="apigateway"),
        param(
            "arn:aws:apigateway:us-east-1::/restapis/abc123def/stages/prod",
            "abc123def",
            id="apigateway-deeper-path-still-extracts-id",
        ),
        param("not-an-arn", None, id="invalid-arn"),
    ],
)
def test_name_from_arn(arn, expected):
    assert _name_from_arn(arn) == expected


# ---------------------------------------------------------------------------
# _classify_apigateway_resource
# ---------------------------------------------------------------------------


@mark.parametrize(
    ("path", "expected"),
    [
        param("/restapis/abc123", "apigateway", id="restapi"),
        param("/restapis/abc123/stages/prod", None, id="stage"),
        param("/restapis/abc123/deployments/xyz", None, id="deployment"),
        param("/restapis/abc123/resources/def", None, id="resource"),
        param("", None, id="empty"),
    ],
)
def test_classify_apigateway_resource(path, expected):
    assert _classify_apigateway_resource(path) == expected


# ---------------------------------------------------------------------------
# API Gateway ARN consistency between discovery and deletion
#
# Name-based and tag-based discovery must produce ARNs that deduplicate
# correctly, and the deletion handler must extract the correct API ID
# from either format.
# ---------------------------------------------------------------------------


def test_apigateway_name_based_arn_format():
    """Name-based scanner uses /restapis/{id} format."""
    # Simulates what _scan_apigateway produces
    arn = "arn:aws:apigateway:us-east-1::/restapis/abc123"
    assert _service_from_arn(arn) == "apigateway"
    assert _name_from_arn(arn) == "abc123"


def test_apigateway_tag_based_arn_dedups_with_name_based():
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


def test_apigateway_api_id_extraction_from_arn():
    """_delete_apigateway logic: split on ::, then parse /restapis/{id}."""
    arn = "arn:aws:apigateway:us-east-1::/restapis/abc123"
    resource_path = arn.split("::", 1)[-1]
    api_id = resource_path.strip("/").split("/")[1]
    assert api_id == "abc123"


# ---------------------------------------------------------------------------
# deduplicate
# ---------------------------------------------------------------------------


def test_deduplicate_removes_duplicate_arns():
    r1 = DiscoveredResource(service="sqs", arn="arn:1", name="q1", region="us-east-1")
    r2 = DiscoveredResource(service="sqs", arn="arn:1", name="q1", region="us-east-1")
    assert len(deduplicate([r1, r2])) == 1


def test_deduplicate_keeps_different_arns():
    r1 = DiscoveredResource(service="sqs", arn="arn:1", name="q1", region="us-east-1")
    r2 = DiscoveredResource(service="sqs", arn="arn:2", name="q2", region="us-east-1")
    assert len(deduplicate([r1, r2])) == 2


def test_deduplicate_keeps_first_occurrence():
    r1 = DiscoveredResource(service="sqs", arn="arn:1", name="tag-name", region="us-east-1")
    r2 = DiscoveredResource(service="sqs", arn="arn:1", name="scan-name", region="us-east-1")
    result = deduplicate([r1, r2])
    assert result[0].name == "tag-name"


def test_deduplicate_empty_list():
    assert deduplicate([]) == []


# ---------------------------------------------------------------------------
# Route53 helpers
# ---------------------------------------------------------------------------


@mark.parametrize(
    ("record_name", "expected"),
    [
        param("stlv-87b3f6-test-api.example.com.", True, id="direct-label"),
        param("_abc.stlv-87b3f6-test-api.example.com.", True, id="nested-label"),
        param("api.example.com.", False, id="non-test"),
        param("stlv-a1b2c3-test", False, id="requires-suffix-after-test"),
    ],
)
def test_is_test_route53_record_name(record_name, expected):
    assert _is_test_route53_record_name(record_name) is expected


def test_route53_record_key_round_trip_with_set_identifier():
    key = _route53_record_key(
        zone_id="Z123",
        record_type="A",
        record_name="stlv-87b3f6-test-api.example.com.",
        set_identifier="blue",
    )
    assert _parse_route53_record_key(key) == (
        "Z123",
        "A",
        "stlv-87b3f6-test-api.example.com.",
        "blue",
    )


def test_parse_route53_record_key_legacy_format():
    assert _parse_route53_record_key("Z123::A::stlv-87b3f6-test-api.example.com.") == (
        "Z123",
        "A",
        "stlv-87b3f6-test-api.example.com.",
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


# ---------------------------------------------------------------------------
# Route53 scan and delete
# ---------------------------------------------------------------------------


def test_scan_route53_records_filters_to_test_names(monkeypatch):
    monkeypatch.setenv("STLV_TEST_DNS_ZONE_ID", "ZTEST")
    pages = [
        {
            "ResourceRecordSets": [
                {"Name": "example.com.", "Type": "NS"},
                {"Name": "example.com.", "Type": "SOA"},
                {
                    "Name": "stlv-87b3f6-test-api.example.com.",
                    "Type": "A",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": "1.1.1.1"}],
                },
                {
                    "Name": "_hash.stlv-87b3f6-test-api.example.com.",
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
    assert results[0].arn == "ZTEST::A::stlv-87b3f6-test-api.example.com.::-"
    assert results[1].arn == "ZTEST::CNAME::_hash.stlv-87b3f6-test-api.example.com.::-"


def test_delete_route53_record_matches_set_identifier():
    pages = [
        {
            "ResourceRecordSets": [
                {
                    "Name": "stlv-87b3f6-test-api.example.com.",
                    "Type": "A",
                    "SetIdentifier": "blue",
                    "TTL": 60,
                    "ResourceRecords": [{"Value": "1.1.1.1"}],
                },
                {
                    "Name": "stlv-87b3f6-test-api.example.com.",
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
            record_name="stlv-87b3f6-test-api.example.com.",
            set_identifier="green",
        ),
        name="stlv-87b3f6-test-api.example.com. [green]",
        region="global",
    )

    _delete_route53_record(session, resource)

    assert len(client.deletions) == 1
    deleted_rrs = client.deletions[0]["ChangeBatch"]["Changes"][0]["ResourceRecordSet"]
    assert deleted_rrs["SetIdentifier"] == "green"


def test_delete_resource_treats_missing_route53_record_as_already_gone():
    client = _FakeRoute53Client([{"ResourceRecordSets": []}])
    session = _FakeSession(route53_client=client)
    resource = DiscoveredResource(
        service="route53",
        arn=_route53_record_key(
            zone_id="ZTEST",
            record_type="A",
            record_name="stlv-87b3f6-test-api.example.com.",
        ),
        name="stlv-87b3f6-test-api.example.com.",
        region="global",
    )

    _delete_resource(session, resource)
    assert client.deletions == []


# ---------------------------------------------------------------------------
# CloudFront delete
# ---------------------------------------------------------------------------


def test_delete_cloudfront_distribution_waits_even_if_already_disabled():
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


# ---------------------------------------------------------------------------
# discover_by_tags
# ---------------------------------------------------------------------------


def _patch_tagging_session(monkeypatch, fake_client: _FakeTaggingClient) -> None:
    def _fake_create_session(profile: str | None, region: str) -> _FakeTaggingSession:
        assert profile is None
        assert region == "us-east-1"
        return _FakeTaggingSession(fake_client)

    monkeypatch.setattr("tests.integration.cleanup_aws._create_session", _fake_create_session)


def test_discover_by_tags_keeps_only_stlv_app_tag(monkeypatch):
    pages = [
        {
            "ResourceTagMappingList": [
                {
                    "ResourceARN": (
                        "arn:aws:lambda:us-east-1:123456789012:function:stlv-a1b2c3-test-fn"
                    ),
                    "Tags": [
                        {"Key": "stelvio:env", "Value": "test"},
                        {"Key": "stelvio:app", "Value": "stlv-a1b2c3"},
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
    _patch_tagging_session(monkeypatch, fake_client)

    result = discover_by_tags(profile=None, regions=["us-east-1"])

    assert len(result) == 1
    assert result[0] == DiscoveredResource(
        service="lambda",
        arn="arn:aws:lambda:us-east-1:123456789012:function:stlv-a1b2c3-test-fn",
        name="stlv-a1b2c3-test-fn",
        region="us-east-1",
    )
    assert fake_client.paginator.paginate_calls == [
        {"TagFilters": [{"Key": "stelvio:env", "Values": ["test"]}]}
    ]


def test_discover_by_tags_skips_non_exact_stlv_prefix(monkeypatch):
    pages = [
        {
            "ResourceTagMappingList": [
                {
                    "ResourceARN": "arn:aws:lambda:us-east-1:123456789012:function:prod-fn",
                    "Tags": [
                        {"Key": "stelvio:env", "Value": "test"},
                        {"Key": "stelvio:app", "Value": "stlv-production"},
                    ],
                }
            ]
        }
    ]
    _patch_tagging_session(monkeypatch, _FakeTaggingClient(pages))

    assert discover_by_tags(profile=None, regions=["us-east-1"]) == []


def test_discover_by_tags_skips_unrecognized_resource_types(monkeypatch):
    pages = [
        {
            "ResourceTagMappingList": [
                {
                    "ResourceARN": (
                        "arn:aws:execute-api:us-east-1:123456789012:abc123/prod/GET/items"
                    ),
                    "Tags": [
                        {"Key": "stelvio:env", "Value": "test"},
                        {"Key": "stelvio:app", "Value": "stlv-a1b2c3"},
                    ],
                }
            ]
        }
    ]
    _patch_tagging_session(monkeypatch, _FakeTaggingClient(pages))

    assert discover_by_tags(profile=None, regions=["us-east-1"]) == []
