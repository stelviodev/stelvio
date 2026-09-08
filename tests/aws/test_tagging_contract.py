from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pulumi
import pytest

from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.api_gateway import ApiDomain, HttpApi, RestApi, WebsocketApi
from stelvio.aws.appsync import AppSync, CognitoAuth
from stelvio.aws.cloudfront.cloudfront import CloudFrontDistribution
from stelvio.aws.cloudfront.origins.components.url import Url
from stelvio.aws.cloudfront.router import Router
from stelvio.aws.cognito.identity_pool import IdentityPool
from stelvio.aws.cognito.types import IdentityPoolBinding
from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoSubscription, DynamoTable, FieldType
from stelvio.aws.email import Email
from stelvio.aws.function.function import Function
from stelvio.aws.queue import Queue, QueueSubscription
from stelvio.aws.s3.s3 import Bucket, BucketNotifySubscription
from stelvio.aws.s3.s3_static_website import S3StaticWebsite
from stelvio.aws.topic import Topic, TopicSubscription
from stelvio.aws.vpc import Vpc
from tests.aws.pulumi_mocks import R

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
    # components with structural tags (e.g. Vpc's Name/stelvio:subnet-type) can't match
    # TAGS exactly; exact=False asserts the user tags are present instead
    exact: bool = True


def _assert_resources_tagged(resources: list, case_id: str, *, exact: bool) -> None:
    assert resources, f"Expected resources for case '{case_id}' but got none"
    if exact:
        assert all(resource.inputs.get("tags") == TAGS for resource in resources)
    else:
        # items() <= is a subset test: every (key, value) pair of TAGS must be
        # present in the resource's tags; structural extras are allowed
        assert all(
            TAGS.items() <= (resource.inputs.get("tags") or {}).items() for resource in resources
        )


# Multi-statement builds/triggers live here; single-expression ones are lambdas in CASES.


def _build_queue_subscription(_: FixtureRequest) -> QueueSubscription:
    queue = Queue("contract-queue-sub", tags=TAGS)
    return queue.subscribe("worker", "functions/simple.handler")


def _build_topic_subscription(_: FixtureRequest) -> TopicSubscription:
    topic = Topic("contract-topic-sub", tags=TAGS)
    return topic.subscribe("worker", "functions/simple.handler")


def _build_dynamo_subscription(_: FixtureRequest) -> DynamoSubscription:
    table = DynamoTable(
        "contract-table-sub",
        fields={"id": FieldType.STRING},
        partition_key="id",
        stream="new-image",
        tags=TAGS,
    )
    return table.subscribe("worker", "functions/simple.handler")


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


def _build_api(_: FixtureRequest) -> RestApi:
    api = RestApi("contract-api", tags=TAGS)
    api.route("GET", "/users", "functions/simple.handler")
    return api


def _build_api_custom_domain(request: FixtureRequest) -> RestApi:
    request.getfixturevalue("app_context_with_dns")
    api = RestApi("contract-api-domain", domain_name="api.example.com", tags=TAGS)
    api.route("GET", "/users", "functions/simple.handler")
    return api


def _trigger_api_custom_domain(component: Any) -> pulumi.Output[Any]:
    return component.resources.base_path_mapping.id


def _build_http_api(_: FixtureRequest) -> HttpApi:
    api = HttpApi("contract-http-api", tags=TAGS)
    api.route("GET", "/users", "functions/simple.handler")
    return api


def _trigger_http_api(component: Any) -> pulumi.Output[Any]:
    resources = component.resources
    outputs = [resources.stage.id]
    outputs.extend(permission.id for permission in resources.permissions)
    outputs.extend(route.id for route in resources.routes)
    if resources.api_mapping is not None:
        outputs.append(resources.api_mapping.id)
    return pulumi.Output.all(*outputs)


def _build_websocket_api(_: FixtureRequest) -> WebsocketApi:
    api = WebsocketApi("contract-websocket-api", tags=TAGS)
    api.route("$connect", "functions/simple.handler")
    return api


def _trigger_websocket_api(component: Any) -> pulumi.Output[Any]:
    resources = component.resources
    return pulumi.Output.all(resources.api.id, resources.stage.id, resources.log_group.id)


def _build_http_api_domain(request: FixtureRequest) -> ApiDomain:
    request.getfixturevalue("app_context_with_dns")
    return ApiDomain("contract-http-api-domain", domain_name="http.example.com", tags=TAGS)


def _trigger_http_api_domain(component: Any) -> pulumi.Output[Any]:
    return component.resources.custom_domain.domain_name


def _build_email(_: FixtureRequest) -> Email:
    return Email("contract-email", "sender@example.com", dmarc=None, tags=TAGS)


def _trigger_email(component: Any) -> pulumi.Output[Any]:
    return pulumi.Output.all(
        component.resources.identity.id, component.resources.configuration_set.id
    )


def _build_acm_validated_domain(request: FixtureRequest) -> AcmValidatedDomain:
    request.getfixturevalue("app_context_with_dns")
    return AcmValidatedDomain("contract-cert", domain_name="api.example.com", tags=TAGS)


def _build_cloudfront_distribution(_: FixtureRequest) -> CloudFrontDistribution:
    bucket = Bucket("contract-cloudfront-bucket")
    return CloudFrontDistribution("contract-cloudfront", bucket=bucket, tags=TAGS)


def _build_router(_: FixtureRequest) -> Router:
    bucket = Bucket("contract-router-bucket")
    router = Router("contract-router", tags=TAGS)
    router.route("/", bucket)
    return router


def _build_s3_static_website(request: FixtureRequest) -> S3StaticWebsite:
    site_dir = Path(request.getfixturevalue("tmp_path")) / "contract-site"
    site_dir.mkdir()
    (site_dir / "index.html").write_text("<h1>Contract</h1>")
    return S3StaticWebsite("contract-static-site", directory=site_dir, tags=TAGS)


def _build_url_origin(_: FixtureRequest) -> Router:
    upstream = Url("contract-upstream", "https://example.com", tags=TAGS)
    router = Router("contract-url-router")
    router.route("/", upstream)
    return router


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


def _build_appsync_custom_domain(request: FixtureRequest) -> AppSync:
    request.getfixturevalue("app_context_with_dns")
    return AppSync(
        "contract-appsync-domain",
        schema=_APPSYNC_SCHEMA,
        auth=CognitoAuth(user_pool_id="us-east-1_ContractPool"),
        domain="appsync.example.com",
        tags=TAGS,
    )


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


def _build_identity_pool(_: FixtureRequest) -> IdentityPool:
    pool = UserPool("contract-id-pool-users", usernames=["email"])
    client = pool.add_client("web")
    return IdentityPool(
        "contract-identity-pool",
        user_pools=[IdentityPoolBinding(user_pool=pool, client=client)],
        tags=TAGS,
    )


def _trigger_vpc(component: Any) -> pulumi.Output[Any]:
    r = component.resources
    return pulumi.Output.all(
        r.vpc.id,
        r.internet_gateway.id,
        *[s.id for s in r.public_subnets + r.private_subnets + r.isolated_subnets],
        *[
            rt.id
            for rt in r.public_route_tables + r.private_route_tables + r.isolated_route_tables
        ],
    )


CASES: tuple[TagCase, ...] = (
    TagCase(
        "function",
        lambda _: Function("contract-function", handler="functions/simple.handler", tags=TAGS),
        lambda c: pulumi.Output.all(c.resources.function.arn, c.resources.role.arn),
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "queue",
        lambda _: Queue("contract-queue", tags=TAGS),
        lambda c: c.resources.queue.arn,
        (lambda m: m.created_sqs_queues(),),
    ),
    TagCase(
        "queue-subscription",
        _build_queue_subscription,
        lambda c: c.resources.event_source_mapping.arn,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "topic",
        lambda _: Topic("contract-topic", tags=TAGS),
        lambda c: c.resources.topic.arn,
        (lambda m: m.created_topics(),),
    ),
    TagCase(
        "topic-subscription",
        _build_topic_subscription,
        lambda c: c.resources.subscription.arn,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "dynamo-table",
        lambda _: DynamoTable(
            "contract-table", fields={"id": FieldType.STRING}, partition_key="id", tags=TAGS
        ),
        lambda c: c.resources.table.arn,
        (lambda m: m.created_dynamo_tables(),),
    ),
    TagCase(
        "dynamo-subscription",
        _build_dynamo_subscription,
        lambda c: c.resources.event_source_mapping.arn,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "bucket",
        lambda _: Bucket("contract-bucket", tags=TAGS),
        lambda c: c.resources.bucket.arn,
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
        lambda _: Cron("contract-cron", "rate(1 day)", "functions/simple.handler", tags=TAGS),
        lambda c: pulumi.Output.all(
            c.resources.rule.arn, c.resources.function.resources.function.arn
        ),
        (
            lambda m: m.created_event_rules(),
            lambda m: m.created_functions(),
            lambda m: m.created_roles(),
        ),
    ),
    TagCase(
        "api",
        _build_api,
        lambda c: c.resources.stage.invoke_url,
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
        "http-api",
        _build_http_api,
        _trigger_http_api,
        (
            lambda m: m.created(R.HTTP_API),
            lambda m: m.created(R.HTTP_API_STAGE),
            lambda m: m.created_log_groups(),
            lambda m: m.created_functions(),
        ),
    ),
    TagCase(
        "websocket-api",
        _build_websocket_api,
        _trigger_websocket_api,
        (
            lambda m: m.created(R.HTTP_API),
            lambda m: m.created(R.HTTP_API_STAGE),
            lambda m: m.created_log_groups(),
            lambda m: m.created_functions(),
        ),
    ),
    TagCase(
        "http-api-domain",
        _build_http_api_domain,
        _trigger_http_api_domain,
        (lambda m: m.created(R.HTTP_API_DOMAIN_NAME), lambda m: m.created_certificates()),
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
        lambda c: c.resources.certificate.arn,
        (lambda m: m.created_certificates(),),
    ),
    TagCase(
        "cloudfront-distribution",
        _build_cloudfront_distribution,
        lambda c: c.resources.distribution.arn,
        (lambda m: m.created_cloudfront_distributions(),),
    ),
    TagCase(
        "router",
        _build_router,
        lambda c: c.resources.distribution.arn,
        (lambda m: m.created_cloudfront_distributions(),),
    ),
    TagCase(
        "s3-static-website",
        _build_s3_static_website,
        lambda c: c.resources.cloudfront_distribution.resources.distribution.arn,
        (lambda m: m.created_s3_buckets(), lambda m: m.created_cloudfront_distributions()),
    ),
    TagCase(
        "url-origin",
        _build_url_origin,
        lambda c: c.resources.distribution.arn,
        (lambda m: m.created_functions(), lambda m: m.created_roles()),
    ),
    TagCase(
        "appsync",
        lambda _: AppSync(
            "contract-appsync",
            schema=_APPSYNC_SCHEMA,
            auth=CognitoAuth(user_pool_id="us-east-1_ContractPool"),
            tags=TAGS,
        ),
        lambda c: c.resources.api.arn,
        (lambda m: m.created_appsync_apis(),),
    ),
    TagCase(
        "appsync-custom-domain",
        _build_appsync_custom_domain,
        lambda c: pulumi.Output.all(
            c.resources.api.arn,
            c.resources.acm_validated_domain.resources.certificate.arn,
        ),
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
        "vpc",
        lambda _: Vpc("contract-vpc", tags=TAGS),
        _trigger_vpc,
        (
            lambda m: [r for r in m.created_resources if r.typ == R.VPC],
            lambda m: [r for r in m.created_resources if r.typ == R.SUBNET],
            lambda m: [r for r in m.created_resources if r.typ == R.INTERNET_GATEWAY],
            lambda m: [r for r in m.created_resources if r.typ == R.ROUTE_TABLE],
        ),
        exact=False,
    ),
    TagCase(
        "user-pool",
        lambda _: UserPool("contract-pool", usernames=["email"], tags=TAGS),
        lambda c: c.resources.user_pool.arn,
        (lambda m: m.created_user_pools(),),
    ),
    TagCase(
        "identity-pool",
        _build_identity_pool,
        lambda c: c.resources.roles_attachment.identity_pool_id,
        (lambda m: m.created_identity_pools(),),
    ),
)


pytestmark = pytest.mark.usefixtures("project_cwd")


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.id)
@pulumi.runtime.test
def test_component_tagging_contract(pulumi_mocks, case: TagCase, request: FixtureRequest):
    component = case.build(request)

    def check(_: Any) -> None:
        for selector in case.selectors:
            _assert_resources_tagged(selector(pulumi_mocks), case.id, exact=case.exact)

    case.trigger(component).apply(check)
