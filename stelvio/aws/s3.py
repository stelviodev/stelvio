from dataclasses import dataclass
from typing import final

import pulumi
import pulumi_aws
from pulumi import Output

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig


@dataclass(frozen=True)
class S3BucketResources:
    bucket: pulumi_aws.s3.Bucket


@final
class Bucket(Component[S3BucketResources], Linkable):
    def __init__(
        self,
        name: str,
        storage_class: str = "STANDARD",
        versioning_enabled: bool = False,
        public_access_block: bool = True,
        enforce_https: bool = True,
        custom_domain: str | None = None,
    ):
        super().__init__(name)
        self.storage_class = storage_class
        self.versioning_enabled = versioning_enabled
        self.public_access_block = public_access_block
        self.enforce_https = enforce_https
        self.custom_domain = custom_domain
        self._resources = None

    @property
    def arn(self) -> Output[str]:
        return self.resources.bucket.arn

    def _create_resources(self) -> S3BucketResources:
        bucket = pulumi_aws.s3.Bucket(
            context().prefix(self.name),
            # acl="private" if self.public_access_block else "public-read",
            versioning={"enabled": self.versioning_enabled},
            # Enforce HTTPS if specified
            # Storage Class
            website={
                "indexDocument": "index.html",
                "errorDocument": "error.html",
            }
        )
        if self.custom_domain:
            bucket.domain_name = self.custom_domain
            acm_validated_domain = AcmValidatedDomain(
                name=self.custom_domain,
                domain_name=self.custom_domain,
            )

            bucket.x509_certificate_arn = acm_validated_domain.resources.certificate.arn
            record = context().dns.create_record(
                resource_name=f"s3bucket_{self.name}-custom-domain-record",
                name=self.custom_domain,
                record_type="CNAME",
                value=bucket.website_endpoint,
                # value=bucket.bucket,  # Use bucket name for CNAME
            )
            pulumi.export(f"s3bucket_{self.name}_custom_domain_record", record.name)


        pulumi.export(f"s3bucket_{self.name}_arn", bucket.arn)
        pulumi.export(f"s3bucket_{self.name}_name", bucket.bucket)
        pulumi.export(f"s3bucket_{self.name}_website_endpoint", bucket.website_endpoint)
        return S3BucketResources(bucket)

    def link(self) -> Link:
        link_creator_ = ComponentRegistry.get_link_config_creator(type(self))

        link_config = link_creator_(self._resources.bucket)
        return Link(self.name, link_config.properties, link_config.permissions)


@link_config_creator(Bucket)
def default_bucket_link(bucket: pulumi_aws.s3.Bucket) -> LinkConfig:
    return LinkConfig(
        properties={"bucket_arn": bucket.arn, "bucket_name": bucket.bucket},
        permissions=[
            AwsPermission(
                actions=["s3:ListBucket"],
                resources=[bucket.arn],
            ),
            AwsPermission(
                actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                resources=[bucket.arn.apply(lambda arn: f"{arn}/*")],
            ),
        ],
    )
