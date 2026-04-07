"""Test helpers to export component properties for integration test assertions.

Integration tests need resource identifiers (ARNs, names, URLs) to verify AWS
resources were created correctly. Since components no longer auto-export these
values, each test explicitly exports what it needs via these helpers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from stelvio import export_output

if TYPE_CHECKING:
    from stelvio.aws.api_gateway import Api
    from stelvio.aws.appsync import AppSync
    from stelvio.aws.cloudfront import CloudFrontDistribution, Router
    from stelvio.aws.cron import Cron
    from stelvio.aws.dynamo_db import DynamoTable
    from stelvio.aws.email import Email
    from stelvio.aws.function import Function
    from stelvio.aws.layer import Layer
    from stelvio.aws.queue import Queue
    from stelvio.aws.s3 import Bucket
    from stelvio.aws.s3.s3_static_website import S3StaticWebsite
    from stelvio.aws.topic import Topic


def export_function(fn: Function) -> None:
    r = fn.resources
    export_output(f"function_{fn.name}_arn", r.function.arn)
    export_output(f"function_{fn.name}_name", r.function.name)
    export_output(f"function_{fn.name}_role_arn", r.role.arn)
    export_output(f"function_{fn.name}_role_name", r.role.name)
    if r.function_url is not None:
        export_output(f"function_{fn.name}_url", r.function_url.function_url)


def export_api(api: Api) -> None:
    r = api.resources
    export_output(f"api_{api.name}_arn", r.rest_api.arn)
    export_output(f"api_{api.name}_id", r.rest_api.id)
    export_output(f"api_{api.name}_invoke_url", r.stage.invoke_url)
    export_output(f"api_{api.name}_stage_name", r.stage.stage_name)


def export_dynamo_table(table: DynamoTable) -> None:
    r = table.resources
    export_output(f"dynamotable_{table.name}_arn", r.table.arn)
    export_output(f"dynamotable_{table.name}_name", r.table.name)
    if table.stream_arn is not None:
        export_output(f"dynamotable_{table.name}_stream_arn", table.stream_arn)


def export_queue(queue: Queue) -> None:
    r = queue.resources
    export_output(f"queue_{queue.name}_arn", r.queue.arn)
    export_output(f"queue_{queue.name}_url", r.queue.url)
    export_output(f"queue_{queue.name}_name", r.queue.name)


def export_topic(topic: Topic) -> None:
    r = topic.resources
    export_output(f"topic_{topic.name}_arn", r.topic.arn)
    export_output(f"topic_{topic.name}_name", r.topic.name)


def export_bucket(bucket: Bucket) -> None:
    r = bucket.resources
    export_output(f"s3bucket_{bucket.name}_arn", r.bucket.arn)
    export_output(f"s3bucket_{bucket.name}_name", r.bucket.bucket)


def export_layer(layer: Layer) -> None:
    r = layer.resources
    export_output(f"layer_{layer.name}_name", r.layer_version.layer_name)
    export_output(f"layer_{layer.name}_version_arn", r.layer_version.arn)


def export_cron(cron: Cron) -> None:
    r = cron.resources
    export_output(f"cron_{cron.name}_rule_arn", r.rule.arn)
    export_output(f"cron_{cron.name}_rule_name", r.rule.name)


def export_email(email: Email) -> None:
    r = email.resources
    export_output(f"email_{email.name}_ses_identity_arn", r.identity.arn)
    export_output(f"email_{email.name}_ses_configuration_set_arn", r.configuration_set.arn)
    if r.verification is not None:
        export_output(f"email_{email.name}_ses_domain_verification_token_arn", r.verification.arn)
    if r.dkim_records:
        for i, record in enumerate(r.dkim_records):
            export_output(f"email_{email.name}_dkim_record_{i}_name", record.name)
            export_output(f"email_{email.name}_dkim_record_{i}_value", record.value)
    if r.dmarc_record is not None:
        export_output(f"email_{email.name}_dmarc_record_name", r.dmarc_record.name)
        export_output(f"email_{email.name}_dmarc_record_value", r.dmarc_record.value)


def export_cloudfront(cf: CloudFrontDistribution) -> None:
    r = cf.resources
    export_output(f"cloudfront_{cf.name}_distribution_id", r.distribution.id)
    export_output(f"cloudfront_{cf.name}_domain_name", r.distribution.domain_name)
    export_output(f"cloudfront_{cf.name}_arn", r.distribution.arn)
    export_output(f"cloudfront_{cf.name}_bucket_policy", r.bucket_policy.id)
    if r.record is not None:
        export_output(f"cloudfront_{cf.name}_record_name", r.record.pulumi_resource.name)


def export_router(router: Router) -> None:
    r = router.resources
    export_output(f"router_{router.name}_distribution_id", r.distribution.id)
    export_output(f"router_{router.name}_domain_name", r.distribution.domain_name)
    export_output(f"router_{router.name}_num_origins", len(router.routes))


def export_s3_static_website(site: S3StaticWebsite) -> None:
    r = site.resources
    export_output(f"s3_static_website_{site.name}_bucket_name", r.bucket.resources.bucket.bucket)
    export_output(
        f"cloudfront_{site.name}-cloudfront_arn",
        r.cloudfront_distribution.resources.distribution.arn,
    )


def export_appsync(api: AppSync) -> None:
    r = api.resources
    export_output(f"appsync_{api.name}_url", api.url)
    export_output(f"appsync_{api.name}_arn", r.api.arn)
    export_output(f"appsync_{api.name}_id", r.api.id)
    if r.api_key is not None:
        export_output(f"appsync_{api.name}_api_key", r.api_key.key)
