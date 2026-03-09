from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pulumi
import pytest

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway.api import Api
from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.aws.cloudfront.cloudfront import CloudFrontDistribution
from stelvio.aws.cloudfront.origins.components.url import Url
from stelvio.aws.cloudfront.router import Router
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoSubscription, DynamoTable, FieldType
from stelvio.aws.email import Email
from stelvio.aws.function.function import Function
from stelvio.aws.queue import Queue, QueueSubscription
from stelvio.aws.s3.s3 import Bucket, BucketNotifySubscription
from stelvio.aws.s3.s3_static_website import S3StaticWebsite
from stelvio.aws.topic import Topic, TopicSubscription

if TYPE_CHECKING:
    from collections.abc import Callable

    from _pytest.fixtures import FixtureRequest

    from tests.aws.pulumi_mocks import PulumiTestMocks

TAGS = {"Team": "platform"}


@dataclass(frozen=True)
class TagCase:
    id: str
    build: Callable[[FixtureRequest], Any]
    trigger: Callable[[Any], pulumi.Output[Any]]
    selectors: tuple[Callable[[PulumiTestMocks], list], ...]


def _assert_resources_tagged(resources: list, case_id: str) -> None:
    assert resources, f"Expected resources for case '{case_id}' but got none"
    assert all(resource.inputs.get("tags") == TAGS for resource in resources)


def _build_function(_: FixtureRequest) -> Function:
    return Function("contract-function", handler="functions/simple.handler", tags=TAGS)


def _trigger_function(component: Any) -> pulumi.Output[Any]:
    return pulumi.Output.all(component.resources.function.arn, component.resources.role.arn)


def _build_queue(_: FixtureRequest) -> Queue:
    return Queue("contract-queue", tags=TAGS)


def _trigger_queue(component: Any) -> pulumi.Output[Any]:
    return component.resources.queue.arn


def _build_queue_subscription(_: FixtureRequest) -> QueueSubscription:
    queue = Queue("contract-queue-sub", tags=TAGS)
    return queue.subscribe("worker", "functions/simple.handler")


def _trigger_queue_subscription(component: Any) -> pulumi.Output[Any]:
    return component.resources.event_source_mapping.arn


def _build_topic(_: FixtureRequest) -> Topic:
    return Topic("contract-topic", tags=TAGS)


def _trigger_topic(component: Any) -> pulumi.Output[Any]:
    return component.resources.topic.arn


def _build_topic_subscription(_: FixtureRequest) -> TopicSubscription:
    topic = Topic("contract-topic-sub", tags=TAGS)
    return topic.subscribe("worker", "functions/simple.handler")


def _trigger_topic_subscription(component: Any) -> pulumi.Output[Any]:
    return component.resources.subscription.arn


def _build_dynamo_table(_: FixtureRequest) -> DynamoTable:
    return DynamoTable(
        "contract-table",
        fields={"id": FieldType.STRING},
        partition_key="id",
        tags=TAGS,
    )


def _trigger_dynamo_table(component: Any) -> pulumi.Output[Any]:
    return component.resources.table.arn


def _build_dynamo_subscription(_: FixtureRequest) -> DynamoSubscription:
    table = DynamoTable(
        "contract-table-sub",
        fields={"id": FieldType.STRING},
        partition_key="id",
        stream="new-image",
        tags=TAGS,
    )
    return table.subscribe("worker", "functions/simple.handler")


def _trigger_dynamo_subscription(component: Any) -> pulumi.Output[Any]:
    return component.resources.event_source_mapping.arn


def _build_bucket(_: FixtureRequest) -> Bucket:
    return Bucket("contract-bucket", tags=TAGS)


def _trigger_bucket(component: Any) -> pulumi.Output[Any]:
    return component.resources.bucket.arn


def _build_bucket_notify_subscription(
    _: FixtureRequest,
) -> tuple[Bucket, BucketNotifySubscription]:
    bucket = Bucket("contract-bucket-notify", tags=TAGS)
    subscription = bucket.notify_function(
        "on-upload",
        events=["s3:ObjectCreated:*"],
        function="functions/simple.handler",
    )
    return bucket, subscription


def _trigger_bucket_notify_subscription(component: Any) -> pulumi.Output[Any]:
    bucket, _ = component
    return pulumi.Output.all(bucket.resources.bucket.arn, bucket.resources.bucket_notification.id)


def _build_cron(_: FixtureRequest) -> Cron:
    return Cron(
        "contract-cron",
        "rate(1 day)",
        "functions/simple.handler",
        tags=TAGS,
    )


def _trigger_cron(component: Any) -> pulumi.Output[Any]:
    return pulumi.Output.all(
        component.resources.rule.arn, component.resources.function.resources.function.arn
    )


def _build_api(_: FixtureRequest) -> Api:
    api = Api("contract-api", tags=TAGS)
    api.route("GET", "/users", "functions/simple.handler")
    return api


def _trigger_api(component: Any) -> pulumi.Output[Any]:
    return component.resources.stage.invoke_url


def _build_api_custom_domain(request: FixtureRequest) -> Api:
    request.getfixturevalue("app_context_with_dns")
    api = Api("contract-api-domain", domain_name="api.example.com", tags=TAGS)
    api.route("GET", "/users", "functions/simple.handler")
    return api


def _trigger_api_custom_domain(component: Any) -> pulumi.Output[Any]:
    return component.resources.stage.invoke_url


def _build_email(_: FixtureRequest) -> Email:
    return Email("contract-email", "sender@example.com", dmarc=None, tags=TAGS)


def _trigger_email(component: Any) -> pulumi.Output[Any]:
    return pulumi.Output.all(
        component.resources.identity.id, component.resources.configuration_set.id
    )


def _build_acm_validated_domain(request: FixtureRequest) -> AcmValidatedDomain:
    request.getfixturevalue("app_context_with_dns")
    return AcmValidatedDomain("contract-cert", domain_name="api.example.com", tags=TAGS)


def _trigger_acm_validated_domain(component: Any) -> pulumi.Output[Any]:
    return component.resources.certificate.arn


def _build_cloudfront_distribution(_: FixtureRequest) -> CloudFrontDistribution:
    bucket = Bucket("contract-cloudfront-bucket")
    return CloudFrontDistribution("contract-cloudfront", bucket=bucket, tags=TAGS)


def _trigger_cloudfront_distribution(component: Any) -> pulumi.Output[Any]:
    return component.resources.distribution.arn


def _build_router(_: FixtureRequest) -> Router:
    bucket = Bucket("contract-router-bucket")
    router = Router("contract-router", tags=TAGS)
    router.route("/", bucket)
    return router


def _trigger_router(component: Any) -> pulumi.Output[Any]:
    return component.resources.distribution.arn


def _build_s3_static_website(request: FixtureRequest) -> S3StaticWebsite:
    site_dir = Path(request.getfixturevalue("tmp_path")) / "contract-site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<h1>Contract</h1>")
    return S3StaticWebsite("contract-static-site", directory=site_dir, tags=TAGS)


def _trigger_s3_static_website(component: Any) -> pulumi.Output[Any]:
    return component.resources.cloudfront_distribution.resources.distribution.arn


def _build_url_origin(_: FixtureRequest) -> Router:
    upstream = Url("contract-upstream", "https://example.com", tags=TAGS)
    router = Router("contract-url-router")
    router.route("/", upstream)
    return router


def _trigger_url_origin(component: Any) -> pulumi.Output[Any]:
    return component.resources.distribution.arn


_APPSYNC_SCHEMA = """\
type Query {
    getItem(id: ID!): Item
}

type Mutation {
    createItem(title: String!): Item
}

type Item {
    id: ID!
    title: String!
}
"""


def _build_appsync(_: FixtureRequest) -> AppSync:
    return AppSync(
        "contract-appsync",
        schema=_APPSYNC_SCHEMA,
        auth=CognitoAuth(user_pool_id="us-east-1_ContractPool"),
        tags=TAGS,
    )


def _trigger_appsync(component: Any) -> pulumi.Output[Any]:
    return component.resources.api.arn


def _build_appsync_custom_domain(request: FixtureRequest) -> AppSync:
    request.getfixturevalue("app_context_with_dns")
    return AppSync(
        "contract-appsync-domain",
        schema=_APPSYNC_SCHEMA,
        auth=CognitoAuth(user_pool_id="us-east-1_ContractPool"),
        domain="appsync.example.com",
        tags=TAGS,
    )


def _trigger_appsync_custom_domain(component: Any) -> pulumi.Output[Any]:
    return component.resources.api.arn


def _build_appsync_data_source_lambda(_: FixtureRequest) -> AppSync:
    api = AppSync(
        "contract-appsync-ds",
        schema=_APPSYNC_SCHEMA,
        auth=CognitoAuth(user_pool_id="us-east-1_ContractPool"),
        tags=TAGS,
    )
    posts = api.data_source_lambda("posts", handler="functions/simple.handler")
    api.query("getItem", posts)
    return api


def _trigger_appsync_data_source_lambda(component: Any) -> pulumi.Output[Any]:
    ds = component._data_sources["posts"]
    return pulumi.Output.all(
        component.resources.api.arn,
        ds.resources.service_role.arn,
        ds.resources.function.resources.function.arn,
    )


def _build_user_pool(_: FixtureRequest) -> UserPool:
    return UserPool("contract-pool", usernames=["email"], tags=TAGS)


def _trigger_user_pool(component: Any) -> pulumi.Output[Any]:
    return component.resources.user_pool.arn


CASES: tuple[TagCase, ...] = (
    TagCase(
        "function",
        _build_function,
        _trigger_function,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "queue",
        _build_queue,
        _trigger_queue,
        (lambda m: m.created_sqs_queues(),),
    ),
    TagCase(
        "queue-subscription",
        _build_queue_subscription,
        _trigger_queue_subscription,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "topic",
        _build_topic,
        _trigger_topic,
        (lambda m: m.created_topics(),),
    ),
    TagCase(
        "topic-subscription",
        _build_topic_subscription,
        _trigger_topic_subscription,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "dynamo-table",
        _build_dynamo_table,
        _trigger_dynamo_table,
        (lambda m: m.created_dynamo_tables(),),
    ),
    TagCase(
        "dynamo-subscription",
        _build_dynamo_subscription,
        _trigger_dynamo_subscription,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "bucket",
        _build_bucket,
        _trigger_bucket,
        (lambda m: m.created_s3_buckets(),),
    ),
    TagCase(
        "bucket-notify-subscription",
        _build_bucket_notify_subscription,
        _trigger_bucket_notify_subscription,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "cron",
        _build_cron,
        _trigger_cron,
        (
            lambda m: m.created_event_rules(),
            lambda m: m.created_functions(),
            lambda m: m.created_roles(),
        ),
    ),
    TagCase(
        "api",
        _build_api,
        _trigger_api,
        (
            lambda m: m.created_rest_apis(),
            lambda m: m.created_stages(),
            lambda m: m.created_functions(),
        ),
    ),
    TagCase(
        "api-custom-domain",
        _build_api_custom_domain,
        _trigger_api_custom_domain,
        (lambda m: m.created_domain_names(), lambda m: m.created_certificates()),
    ),
    TagCase(
        "email",
        _build_email,
        _trigger_email,
        (lambda m: m.created_email_identities(), lambda m: m.created_configuration_sets()),
    ),
    TagCase(
        "acm-validated-domain",
        _build_acm_validated_domain,
        _trigger_acm_validated_domain,
        (lambda m: m.created_certificates(),),
    ),
    TagCase(
        "cloudfront-distribution",
        _build_cloudfront_distribution,
        _trigger_cloudfront_distribution,
        (lambda m: m.created_cloudfront_distributions(),),
    ),
    TagCase(
        "router",
        _build_router,
        _trigger_router,
        (lambda m: m.created_cloudfront_distributions(),),
    ),
    TagCase(
        "s3-static-website",
        _build_s3_static_website,
        _trigger_s3_static_website,
        (lambda m: m.created_s3_buckets(), lambda m: m.created_cloudfront_distributions()),
    ),
    TagCase(
        "url-origin",
        _build_url_origin,
        _trigger_url_origin,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "appsync",
        _build_appsync,
        _trigger_appsync,
        (lambda m: m.created_appsync_apis(),),
    ),
    TagCase(
        "appsync-custom-domain",
        _build_appsync_custom_domain,
        _trigger_appsync_custom_domain,
        (lambda m: m.created_appsync_apis(), lambda m: m.created_certificates()),
    ),
    TagCase(
        "appsync-data-source-lambda",
        _build_appsync_data_source_lambda,
        _trigger_appsync_data_source_lambda,
        (
            lambda m: m.created_appsync_apis(),
            lambda m: m.created_roles(),
            lambda m: m.created_functions(),
        ),
    ),
    TagCase(
        "user-pool",
        _build_user_pool,
        _trigger_user_pool,
        (lambda m: m.created_user_pools(),),
    ),
)


pytestmark = pytest.mark.usefixtures("project_cwd")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
@pulumi.runtime.test
def test_component_tagging_contract(pulumi_mocks, case: TagCase, request: FixtureRequest):
    component = case.build(request)

    def check(_: Any) -> None:
        for selector in case.selectors:
            _assert_resources_tagged(selector(pulumi_mocks), case.id)

    case.trigger(component).apply(check)
