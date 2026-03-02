"""AWS resource discovery and deletion for orphaned integration test cleanup.

Level 2 (tag-based): Uses Resource Groups Tagging API to find all resources
tagged with stelvio:env=test and stelvio:app matching "stlv-<6hex>".

Level 3 (name-prefix): Scans per-service list APIs for resources whose names
match the test naming pattern (stlv-<hex>-test-*).

Both levels produce DiscoveredResource instances that can be deduplicated by
ARN and deleted in the correct dependency order.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import boto3

# Test resource naming: stlv-{6 hex chars}-test-*
_NAME_PREFIX_RE = re.compile(r"^stlv-[0-9a-f]{6}-test-")
_TEST_APP_TAG_RE = re.compile(r"^stlv-[0-9a-f]{6}$")

# Deletion phases — ordered by dependency
_DELETION_PHASES = [
    ("Lambda functions", ["lambda"]),
    ("Lambda layers", ["lambda-layer"]),
    ("EventBridge rules", ["events"]),
    ("SQS queues, SNS topics, DynamoDB tables", ["sqs", "sns", "dynamodb"]),
    ("CloudFront distributions", ["cloudfront"]),
    ("S3 buckets", ["s3"]),
    ("API Gateway REST APIs", ["apigateway"]),
    ("IAM roles and policies", ["iam-role", "iam-policy"]),
    ("ACM certificates and SES", ["acm", "ses-identity", "ses-config-set"]),
    ("Route53 records", ["route53"]),
]


@dataclass(frozen=True)
class DiscoveredResource:
    """A resource found by tag or name scanning."""

    service: str
    arn: str
    name: str
    region: str


def _create_session(profile: str | None, region: str) -> boto3.Session:
    return boto3.Session(profile_name=profile, region_name=region)


# ---------------------------------------------------------------------------
# Level 2: Tag-based discovery
# ---------------------------------------------------------------------------


def discover_by_tags(profile: str | None, regions: list[str]) -> list[DiscoveredResource]:
    """Find resources tagged as integration tests (env=test, app=stlv-<6hex>)."""
    results: list[DiscoveredResource] = []

    for region in regions:
        session = _create_session(profile, region)
        client = session.client("resourcegroupstaggingapi")
        paginator = client.get_paginator("get_resources")

        for page in paginator.paginate(TagFilters=[{"Key": "stelvio:env", "Values": ["test"]}]):
            for item in page.get("ResourceTagMappingList", []):
                arn = item["ResourceARN"]
                tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
                app_tag = tags.get("stelvio:app", "")

                if not _is_test_app_tag(app_tag):
                    continue

                service = _service_from_arn(arn)
                name = _name_from_arn(arn)
                if service and name:
                    results.append(
                        DiscoveredResource(service=service, arn=arn, name=name, region=region)
                    )

    return results


def _classify_iam_resource(resource: str) -> str | None:
    if resource.startswith("policy/"):
        return "iam-policy"
    if resource.startswith("role/"):
        return "iam-role"
    return None


def _classify_apigateway_resource(resource: str) -> str | None:
    """Only match top-level REST APIs, not stages/deployments/resources."""
    segments = resource.strip("/").split("/")
    if len(segments) == 2 and segments[0] == "restapis":
        return "apigateway"
    return None


def _service_from_arn(arn: str) -> str | None:
    """Map an ARN to our internal service category."""
    parts = arn.split(":")
    if len(parts) < 6:
        return None

    service = parts[2]
    resource = ":".join(parts[5:])

    mapping = {
        "lambda": "lambda-layer" if resource.startswith("layer:") else "lambda",
        "iam": _classify_iam_resource(resource),
        "sqs": "sqs",
        "sns": "sns",
        "dynamodb": "dynamodb",
        "s3": "s3",
        "events": "events",
        "apigateway": _classify_apigateway_resource(resource),
        "acm": "acm",
        "ses": "ses-identity" if "identity" in resource else "ses-config-set",
        "cloudfront": "cloudfront",
        "route53": "route53",
    }
    return mapping.get(service)


def _name_from_arn(arn: str) -> str | None:
    """Extract a human-readable name from an ARN."""
    parts = arn.split(":")
    if len(parts) < 6:
        return None

    resource = ":".join(parts[5:])

    # Remove common prefixes
    for prefix in (
        "function:",
        "layer:",
        "role/",
        "policy/",
        "table/",
        "rule/",
        "distribution/",
    ):
        if resource.startswith(prefix):
            resource = resource[len(prefix) :]
            break

    # S3 ARN is arn:aws:s3:::bucket-name (no resource part after service)
    if parts[2] == "s3" and not resource:
        return parts[5] if len(parts) > 5 else None

    # API Gateway: arn:aws:apigateway:{region}::/restapis/{id}
    if resource.startswith("/restapis/"):
        return resource.split("/")[2]

    # SNS topic: arn:aws:sns:region:account:topic-name
    # SQS queue: arn:aws:sqs:region:account:queue-name
    # These already have just the name in the resource part

    return resource.split("/")[0] if resource else None


# ---------------------------------------------------------------------------
# Level 3: Name-prefix discovery
# ---------------------------------------------------------------------------


def discover_by_name(profile: str | None, regions: list[str]) -> list[DiscoveredResource]:
    """Find resources whose names match the test naming pattern."""
    results: list[DiscoveredResource] = []

    for region in regions:
        session = _create_session(profile, region)

        _scan_lambda_functions(session, region, results)
        _scan_lambda_layers(session, region, results)
        _scan_sqs_queues(session, region, results)
        _scan_sns_topics(session, region, results)
        _scan_dynamodb_tables(session, region, results)
        _scan_eventbridge_rules(session, region, results)
        _scan_apigateway(session, region, results)

    # IAM is global — scan once regardless of regions
    iam_session = _create_session(profile, regions[0])
    _scan_iam_resources(iam_session, results)

    # S3 listing is global — scan once and filter by target regions
    s3_session = _create_session(profile, regions[0])
    _scan_s3_buckets(s3_session, set(regions), results)

    # CloudFront is global — identify test distributions by tags
    cf_session = _create_session(profile, regions[0])
    _scan_cloudfront_distributions(cf_session, results)

    # Route53 — scan test zone if configured (zone is global)
    r53_session = _create_session(profile, regions[0])
    _scan_route53_records(r53_session, results)

    return results


def _matches_name(name: str) -> bool:
    return bool(_NAME_PREFIX_RE.match(name))


def _is_test_app_tag(app_tag: str) -> bool:
    return bool(_TEST_APP_TAG_RE.match(app_tag))


def _scan_lambda_functions(
    session: boto3.Session, region: str, results: list[DiscoveredResource]
) -> None:
    client = session.client("lambda")
    paginator = client.get_paginator("list_functions")
    for page in paginator.paginate():
        results.extend(
            DiscoveredResource(
                service="lambda",
                arn=fn["FunctionArn"],
                name=fn["FunctionName"],
                region=region,
            )
            for fn in page["Functions"]
            if _matches_name(fn["FunctionName"])
        )


def _scan_lambda_layers(
    session: boto3.Session, region: str, results: list[DiscoveredResource]
) -> None:
    client = session.client("lambda")
    paginator = client.get_paginator("list_layers")
    for page in paginator.paginate():
        results.extend(
            DiscoveredResource(
                service="lambda-layer",
                arn=layer["LayerArn"],
                name=layer["LayerName"],
                region=region,
            )
            for layer in page["Layers"]
            if _matches_name(layer["LayerName"])
        )


def _scan_iam_resources(session: boto3.Session, results: list[DiscoveredResource]) -> None:
    """Scan IAM roles and customer-managed policies. IAM is global."""
    iam = session.client("iam")

    # Roles
    paginator = iam.get_paginator("list_roles")
    for page in paginator.paginate():
        results.extend(
            DiscoveredResource(
                service="iam-role",
                arn=role["Arn"],
                name=role["RoleName"],
                region="global",
            )
            for role in page["Roles"]
            if _matches_name(role["RoleName"])
        )

    # Customer-managed policies only
    paginator = iam.get_paginator("list_policies")
    for page in paginator.paginate(Scope="Local"):
        results.extend(
            DiscoveredResource(
                service="iam-policy",
                arn=policy["Arn"],
                name=policy["PolicyName"],
                region="global",
            )
            for policy in page["Policies"]
            if _matches_name(policy["PolicyName"])
        )


def _scan_sqs_queues(
    session: boto3.Session, region: str, results: list[DiscoveredResource]
) -> None:
    client = session.client("sqs")
    # SQS list_queues doesn't have a paginator in older boto3
    response = client.list_queues(QueueNamePrefix="stlv-")
    for url in response.get("QueueUrls", []):
        name = url.rsplit("/", 1)[-1]
        if _matches_name(name):
            # Build ARN from queue URL
            # URL format: https://sqs.{region}.amazonaws.com/{account}/{name}
            parts = url.replace("https://", "").split("/")
            account = parts[1] if len(parts) > 1 else "unknown"
            arn = f"arn:aws:sqs:{region}:{account}:{name}"
            results.append(DiscoveredResource(service="sqs", arn=arn, name=name, region=region))


def _scan_sns_topics(
    session: boto3.Session, region: str, results: list[DiscoveredResource]
) -> None:
    client = session.client("sns")
    paginator = client.get_paginator("list_topics")
    for page in paginator.paginate():
        for topic in page["Topics"]:
            arn = topic["TopicArn"]
            name = arn.rsplit(":", 1)[-1]
            if _matches_name(name):
                results.append(
                    DiscoveredResource(service="sns", arn=arn, name=name, region=region)
                )


def _scan_dynamodb_tables(
    session: boto3.Session, region: str, results: list[DiscoveredResource]
) -> None:
    client = session.client("dynamodb")
    paginator = client.get_paginator("list_tables")
    account_id = session.client("sts").get_caller_identity()["Account"]
    for page in paginator.paginate():
        for table_name in page["TableNames"]:
            if _matches_name(table_name):
                arn = f"arn:aws:dynamodb:{region}:{account_id}:table/{table_name}"
                results.append(
                    DiscoveredResource(service="dynamodb", arn=arn, name=table_name, region=region)
                )


def _scan_s3_buckets(
    session: boto3.Session, regions: set[str], results: list[DiscoveredResource]
) -> None:
    """Scan S3 buckets. Listing is global, filter to target regions."""
    client = session.client("s3")
    response = client.list_buckets()
    for bucket in response["Buckets"]:
        name = bucket["Name"]
        if not _matches_name(name):
            continue
        try:
            loc = client.get_bucket_location(Bucket=name)
            bucket_region = loc["LocationConstraint"] or "us-east-1"
        except client.exceptions.NoSuchBucket:
            continue
        if bucket_region in regions:
            arn = f"arn:aws:s3:::{name}"
            results.append(
                DiscoveredResource(service="s3", arn=arn, name=name, region=bucket_region)
            )


def _scan_eventbridge_rules(
    session: boto3.Session, region: str, results: list[DiscoveredResource]
) -> None:
    client = session.client("events")
    paginator = client.get_paginator("list_rules")
    for page in paginator.paginate():
        results.extend(
            DiscoveredResource(
                service="events",
                arn=rule["Arn"],
                name=rule["Name"],
                region=region,
            )
            for rule in page["Rules"]
            if _matches_name(rule["Name"])
        )


def _scan_apigateway(
    session: boto3.Session, region: str, results: list[DiscoveredResource]
) -> None:
    client = session.client("apigateway")
    paginator = client.get_paginator("get_rest_apis")
    for page in paginator.paginate():
        results.extend(
            DiscoveredResource(
                service="apigateway",
                arn=f"arn:aws:apigateway:{region}::/restapis/{api['id']}",
                name=api["name"],
                region=region,
            )
            for api in page["items"]
            if _matches_name(api["name"])
        )


def _scan_cloudfront_distributions(
    session: boto3.Session, results: list[DiscoveredResource]
) -> None:
    """Find test CloudFront distributions by checking tags for stelvio:env=test."""
    client = session.client("cloudfront")
    paginator = client.get_paginator("list_distributions")
    for page in paginator.paginate():
        dist_list = page.get("DistributionList", {})
        for dist in dist_list.get("Items", []):
            tags_resp = client.list_tags_for_resource(Resource=dist["ARN"])
            tags = {t["Key"]: t["Value"] for t in tags_resp["Tags"]["Items"]}
            if tags.get("stelvio:env") == "test" and _is_test_app_tag(tags.get("stelvio:app", "")):
                results.append(
                    DiscoveredResource(
                        service="cloudfront",
                        arn=dist["ARN"],
                        name=dist["Id"],
                        region="global",
                    )
                )


def _scan_route53_records(session: boto3.Session, results: list[DiscoveredResource]) -> None:
    """Scan Route53 test zone for orphaned records.

    Route53 records don't have tags, so we scope by DNS name pattern and only
    include records with a test-prefixed label. Only runs if
    STLV_TEST_DNS_ZONE_ID is set.
    """
    zone_id = os.environ.get("STLV_TEST_DNS_ZONE_ID")
    if not zone_id:
        return

    client = session.client("route53")
    paginator = client.get_paginator("list_resource_record_sets")
    for page in paginator.paginate(HostedZoneId=zone_id):
        for rrs in page["ResourceRecordSets"]:
            if rrs["Type"] in ("NS", "SOA"):
                continue
            if not _is_test_route53_record_name(rrs["Name"]):
                continue
            arn = _route53_record_key(
                zone_id=zone_id,
                record_type=rrs["Type"],
                record_name=rrs["Name"],
                set_identifier=rrs.get("SetIdentifier"),
            )
            display_name = (
                f"{rrs['Name']} [{rrs['SetIdentifier']}]"
                if "SetIdentifier" in rrs
                else rrs["Name"]
            )
            results.append(
                DiscoveredResource(
                    service="route53",
                    arn=arn,
                    name=display_name,
                    region="global",
                )
            )


def _is_test_route53_record_name(record_name: str) -> bool:
    """Allow records containing a test-prefixed label within the full name."""
    normalized = record_name.rstrip(".")
    if _matches_name(normalized):
        return True
    return any(_matches_name(label) for label in normalized.split("."))


def _route53_record_key(
    *, zone_id: str, record_type: str, record_name: str, set_identifier: str | None = None
) -> str:
    """Build a unique record key, including optional routing SetIdentifier."""
    encoded_set_identifier = set_identifier if set_identifier is not None else "-"
    return f"{zone_id}::{record_type}::{record_name}::{encoded_set_identifier}"


def _parse_route53_record_key(key: str) -> tuple[str, str, str, str | None]:
    """Parse Route53 key; supports legacy 3-part and current 4-part formats."""
    parts = key.split("::")
    if len(parts) == 3:
        zone_id, record_type, record_name = parts
        return zone_id, record_type, record_name, None
    if len(parts) == 4:
        zone_id, record_type, record_name, encoded_set_identifier = parts
        set_identifier = None if encoded_set_identifier == "-" else encoded_set_identifier
        return zone_id, record_type, record_name, set_identifier
    raise ValueError(f"Invalid Route53 record key: {key}")


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def deduplicate(resources: list[DiscoveredResource]) -> list[DiscoveredResource]:
    """Remove duplicate resources by ARN, keeping the first occurrence."""
    seen: set[str] = set()
    unique: list[DiscoveredResource] = []
    for r in resources:
        if r.arn not in seen:
            seen.add(r.arn)
            unique.append(r)
    return unique


# ---------------------------------------------------------------------------
# Deletion
# ---------------------------------------------------------------------------


def delete_resources(
    profile: str | None,
    resources: list[DiscoveredResource],
) -> tuple[int, int]:
    """Delete resources in phased dependency order. Returns (succeeded, failed)."""
    by_service: dict[str, list[DiscoveredResource]] = {}
    for r in resources:
        by_service.setdefault(r.service, []).append(r)

    sessions: dict[str, boto3.Session] = {}
    succeeded = 0
    failed = 0

    for phase_name, phase_services in _DELETION_PHASES:
        s, f = _run_deletion_phase(phase_name, phase_services, by_service, profile, sessions)
        succeeded += s
        failed += f

    return succeeded, failed


def _run_deletion_phase(
    phase_name: str,
    phase_services: list[str],
    by_service: dict[str, list[DiscoveredResource]],
    profile: str | None,
    sessions: dict[str, boto3.Session],
) -> tuple[int, int]:
    phase_resources = []
    for svc in phase_services:
        phase_resources.extend(by_service.get(svc, []))

    if not phase_resources:
        return 0, 0

    print(f"\n  Phase: {phase_name} ({len(phase_resources)} resources)")
    succeeded = 0
    failed = 0

    for r in phase_resources:
        # IAM uses "global" as region — any region works for the API call
        region = "us-east-1" if r.region == "global" else r.region
        if region not in sessions:
            sessions[region] = _create_session(profile, region)
        session = sessions[region]
        try:
            _delete_resource(session, r)
            succeeded += 1
        except Exception as e:
            print(f"    FAILED {r.service}: {r.name} — {e}")
            failed += 1

    return succeeded, failed


_NOT_FOUND_ERROR_CODES = {
    "NoSuchDistribution",
    "NoSuchEntity",
    "NotFoundException",
    "ResourceNotFoundException",
    "NoSuchBucket",
    "NoSuchHostedZone",
    "QueueDoesNotExist",
    "AWS.SimpleQueueService.NonExistentQueue",
}


def _delete_resource(session: boto3.Session, resource: DiscoveredResource) -> None:
    """Dispatch deletion to the appropriate service handler.

    Treats "not found" errors as success — the resource is already gone
    (common with stale Resource Groups Tagging API entries).
    """
    from botocore.exceptions import ClientError

    handlers = {
        "lambda": _delete_lambda_function,
        "lambda-layer": _delete_lambda_layer,
        "events": _delete_eventbridge_rule,
        "sqs": _delete_sqs_queue,
        "sns": _delete_sns_topic,
        "dynamodb": _delete_dynamodb_table,
        "s3": _delete_s3_bucket,
        "apigateway": _delete_apigateway,
        "iam-role": _delete_iam_role,
        "iam-policy": _delete_iam_policy,
        "acm": _delete_acm_certificate,
        "ses-identity": _delete_ses_identity,
        "ses-config-set": _delete_ses_config_set,
        "cloudfront": _delete_cloudfront_distribution,
        "route53": _delete_route53_record,
    }
    handler = handlers.get(resource.service)
    if handler is None:
        raise ValueError(f"No deletion handler for service: {resource.service}")
    try:
        handler(session, resource)
        print(f"    Deleted {resource.service}: {resource.name}")
    except ClientError as e:
        if e.response["Error"]["Code"] in _NOT_FOUND_ERROR_CODES:
            print(f"    Already gone {resource.service}: {resource.name}")
            return
        raise


def _delete_lambda_function(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("lambda")
    # Delete event source mappings first
    esms = client.list_event_source_mappings(FunctionName=r.name)
    for esm in esms.get("EventSourceMappings", []):
        client.delete_event_source_mapping(UUID=esm["UUID"])
    client.delete_function(FunctionName=r.name)


def _delete_lambda_layer(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("lambda")
    # Delete all versions of the layer
    versions = client.list_layer_versions(LayerName=r.name)
    for v in versions.get("LayerVersions", []):
        client.delete_layer_version(LayerName=r.name, VersionNumber=v["Version"])


def _delete_eventbridge_rule(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("events")
    # Remove all targets first
    targets = client.list_targets_by_rule(Rule=r.name)
    target_ids = [t["Id"] for t in targets.get("Targets", [])]
    if target_ids:
        client.remove_targets(Rule=r.name, Ids=target_ids)
    client.delete_rule(Name=r.name)


def _delete_sqs_queue(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("sqs")
    # Get queue URL from name
    response = client.get_queue_url(QueueName=r.name)
    client.delete_queue(QueueUrl=response["QueueUrl"])


def _delete_sns_topic(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("sns")
    # Delete subscriptions first
    paginator = client.get_paginator("list_subscriptions_by_topic")
    for page in paginator.paginate(TopicArn=r.arn):
        for sub in page["Subscriptions"]:
            if sub["SubscriptionArn"] != "PendingConfirmation":
                client.unsubscribe(SubscriptionArn=sub["SubscriptionArn"])
    client.delete_topic(TopicArn=r.arn)


def _delete_dynamodb_table(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("dynamodb")
    client.delete_table(TableName=r.name)


def _delete_s3_bucket(session: boto3.Session, r: DiscoveredResource) -> None:
    s3 = session.resource("s3")
    bucket = s3.Bucket(r.name)
    # Delete all object versions (handles versioned buckets)
    bucket.object_versions.delete()
    # Delete remaining objects (non-versioned)
    bucket.objects.delete()
    bucket.delete()


def _delete_apigateway(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("apigateway")
    # ARN: arn:aws:apigateway:{region}::/restapis/{id}
    # Split on :: to get resource path, then extract ID from /restapis/{id}
    resource_path = r.arn.split("::", 1)[-1]
    api_id = resource_path.strip("/").split("/")[1]
    client.delete_rest_api(restApiId=api_id)


def _delete_iam_role(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("iam")
    # Detach managed policies
    attached = client.list_attached_role_policies(RoleName=r.name)
    for policy in attached["AttachedPolicies"]:
        client.detach_role_policy(RoleName=r.name, PolicyArn=policy["PolicyArn"])
    # Delete inline policies
    inline = client.list_role_policies(RoleName=r.name)
    for policy_name in inline["PolicyNames"]:
        client.delete_role_policy(RoleName=r.name, PolicyName=policy_name)
    client.delete_role(RoleName=r.name)


def _delete_iam_policy(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("iam")
    # Detach from all entities
    entities = client.list_entities_for_policy(PolicyArn=r.arn)
    for role in entities.get("PolicyRoles", []):
        client.detach_role_policy(RoleName=role["RoleName"], PolicyArn=r.arn)
    for user in entities.get("PolicyUsers", []):
        client.detach_user_policy(UserName=user["UserName"], PolicyArn=r.arn)
    for group in entities.get("PolicyGroups", []):
        client.detach_group_policy(GroupName=group["GroupName"], PolicyArn=r.arn)
    # Delete non-default versions
    versions = client.list_policy_versions(PolicyArn=r.arn)
    for v in versions["Versions"]:
        if not v["IsDefaultVersion"]:
            client.delete_policy_version(PolicyArn=r.arn, VersionId=v["VersionId"])
    client.delete_policy(PolicyArn=r.arn)


def _delete_acm_certificate(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("acm")
    client.delete_certificate(CertificateArn=r.arn)


def _delete_ses_identity(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("sesv2")
    # Extract identity name from ARN
    # arn:aws:ses:{region}:{account}:identity/{identity}
    identity = r.name
    client.delete_email_identity(EmailIdentity=identity)


def _delete_ses_config_set(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("sesv2")
    client.delete_configuration_set(ConfigurationSetName=r.name)


def _delete_cloudfront_distribution(session: boto3.Session, r: DiscoveredResource) -> None:
    client = session.client("cloudfront")
    dist_id = r.name

    response = client.get_distribution_config(Id=dist_id)
    config = response["DistributionConfig"]
    etag = response["ETag"]

    if config["Enabled"]:
        config["Enabled"] = False
        response = client.update_distribution(Id=dist_id, DistributionConfig=config, IfMatch=etag)
        etag = response["ETag"]
        print(f"      Waiting for distribution {dist_id} to disable...")
    else:
        print(f"      Distribution {dist_id} already disabled; waiting for deployed state...")

    # Delete requires Deployed status even if already disabled.
    waiter = client.get_waiter("distribution_deployed")
    waiter.wait(Id=dist_id)

    # Re-fetch ETag after deployment completes.
    response = client.get_distribution(Id=dist_id)
    etag = response["ETag"]

    client.delete_distribution(Id=dist_id, IfMatch=etag)


def _delete_route53_record(session: boto3.Session, r: DiscoveredResource) -> None:
    from botocore.exceptions import ClientError

    zone_id, record_type, record_name, set_identifier = _parse_route53_record_key(r.arn)
    client = session.client("route53")

    # Fetch full zone list to match full identity, including SetIdentifier.
    paginator = client.get_paginator("list_resource_record_sets")
    target_name = record_name if record_name.endswith(".") else f"{record_name}."
    for page in paginator.paginate(HostedZoneId=zone_id):
        for rrs in page["ResourceRecordSets"]:
            current_name = rrs["Name"] if rrs["Name"].endswith(".") else f"{rrs['Name']}."
            if current_name != target_name or rrs["Type"] != record_type:
                continue
            if rrs.get("SetIdentifier") != set_identifier:
                continue
            client.change_resource_record_sets(
                HostedZoneId=zone_id,
                ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": rrs}]},
            )
            return
    raise ClientError(
        {
            "Error": {
                "Code": "ResourceNotFoundException",
                "Message": f"Route53 record not found: {record_name} ({record_type})",
            }
        },
        "ChangeResourceRecordSets",
    )
