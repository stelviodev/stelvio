from dataclasses import dataclass
from typing import final
import os
import hashlib

import pulumi
import pulumi_aws
from pulumi import Output

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.link import Link, Linkable, LinkConfig


@dataclass(frozen=True)
class S3BucketResources:
    bucket: pulumi_aws.s3.Bucket
    cloudfront_distribution: CloudFrontDistribution | None = None


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

    @property
    def cloudfront_domain_name(self) -> Output[str] | None:
        """Returns the CloudFront distribution domain name if custom domain is configured."""
        if self.resources.cloudfront_distribution:
            return self.resources.cloudfront_distribution.domain_name
        return None

    @property
    def website_url(self) -> Output[str]:
        """Returns the website URL (custom domain if configured, otherwise S3 website endpoint)."""
        if self.custom_domain:
            return pulumi.Output.concat("https://", self.custom_domain)
        return self.resources.bucket.website_endpoint

    def _create_resources(self) -> S3BucketResources:
        bucket = pulumi_aws.s3.Bucket(
            context().prefix(self.name),
            versioning={"enabled": self.versioning_enabled},
            website={
                "indexDocument": "index.html",
                "errorDocument": "error.html",
            }
        )

        # Configure public access block
        public_access_block = pulumi_aws.s3.BucketPublicAccessBlock(
            context().prefix(f"{self.name}-pab"),
            bucket=bucket.id,
            block_public_acls=self.public_access_block,
            block_public_policy=self.public_access_block,
            ignore_public_acls=self.public_access_block,
            restrict_public_buckets=self.public_access_block,
        )

        cloudfront_distribution = None
        
        if self.custom_domain:
            # Create ACM certificate for the custom domain
            acm_validated_domain = AcmValidatedDomain(
                name=self.custom_domain,
                domain_name=self.custom_domain,
            )

            # Create CloudFront distribution
            cloudfront_distribution = CloudFrontDistribution(
                name=f"{self.name}-cloudfront",
                s3_bucket=bucket,
                custom_domain=self.custom_domain,
                certificate_arn=acm_validated_domain.resources.certificate.arn,
            )

            # Create DNS record pointing to CloudFront distribution
            record = context().dns.create_record(
                resource_name=f"s3bucket_{self.name}-custom-domain-record",
                name=self.custom_domain,
                record_type="CNAME",
                value=cloudfront_distribution.domain_name,
            )


#             index_html_content = f"""
# <!DOCTYPE html>
# <html lang="en">
# <head>
#     <meta charset="UTF-8">
#     <meta name="viewport" content="width=device-width, initial-scale=1.0">
#     <title>Welcome to {self.custom_domain}</title>
# </head>
# <body>
#     <h1>Welcome to {self.custom_domain}!</h1>
#     <p>This is a static website hosted on S3</p>
#     <p>It was deployed using Stelvio</p>
# </body>
# </html>
# """
#             # Upload index.html to the bucket
#             pulumi_aws.s3.BucketObject(
#                 "index.html",
#                 bucket=bucket.id,
#                 content=index_html_content,
#                 key="index.html",
#                 content_type="text/html",
#             )


            pulumi.export(f"s3bucket_{self.name}_custom_domain_record", record.name)
            pulumi.export(f"s3bucket_{self.name}_cloudfront_domain", cloudfront_distribution.domain_name)

        pulumi.export(f"s3bucket_{self.name}_arn", bucket.arn)
        pulumi.export(f"s3bucket_{self.name}_name", bucket.bucket)
        pulumi.export(f"s3bucket_{self.name}_website_endpoint", bucket.website_endpoint)
        
        return S3BucketResources(bucket, cloudfront_distribution)

    def link(self) -> Link:
        link_creator_ = ComponentRegistry.get_link_config_creator(type(self))

        link_config = link_creator_(self.resources.bucket)
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




@dataclass(frozen=True)
class S3StaticWebsiteResources:
    bucket: Bucket
    files: list[pulumi_aws.s3.BucketObject]
    

@final
class S3StaticWebsite(Component[S3StaticWebsiteResources]):
    def __init__(self, name: str, bucket: Bucket, directory: str):
        super().__init__(name)
        self.bucket = bucket
        self.directory = directory
        self._resources = None

    def _create_resources(self) -> S3StaticWebsiteResources:
        files = []
        ## glob all files in the directory
        for root, _, filenames in os.walk(self.directory):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                key = os.path.relpath(file_path, self.directory)
                
                # Calculate file hash for ETag
                with open(file_path, 'rb') as f:
                    file_content = f.read()
                    file_hash = hashlib.md5(file_content).hexdigest()
                
                # Determine if file is text or binary
                is_text_file = key.endswith(('.html', '.css', '.js', '.txt', '.md', '.json', '.xml', '.svg'))
                
                # Set cache control based on file type
                if key.endswith(('.html', '.htm')):
                    # HTML files should have shorter cache times for content updates
                    cache_control = "public, max-age=300"  # 5 minutes
                elif key.endswith(('.css', '.js')):
                    # CSS/JS can have longer cache but still reasonable for updates
                    cache_control = "public, max-age=3600"  # 1 hour
                elif key.endswith(('.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg')):
                    # Images can have long cache times
                    cache_control = "public, max-age=86400"  # 24 hours
                else:
                    # Default cache control
                    cache_control = "public, max-age=3600"  # 1 hour
                
                if is_text_file:
                    with open(file_path, 'r', encoding='utf-8') as file:
                        content = file.read()
                    
                    # Set appropriate content type for text files
                    if key.endswith('.html'):
                        content_type = "text/html"
                    elif key.endswith('.css'):
                        content_type = "text/css"
                    elif key.endswith('.js'):
                        content_type = "application/javascript"
                    elif key.endswith('.json'):
                        content_type = "application/json"
                    elif key.endswith('.xml'):
                        content_type = "application/xml"
                    elif key.endswith('.svg'):
                        content_type = "image/svg+xml"
                    else:
                        content_type = "text/plain"
                    
                    files.append(
                        pulumi_aws.s3.BucketObject(
                            f"{self.name}-{key}-{file_hash[:8]}",  # Include hash in resource name
                            bucket=self.bucket.resources.bucket.id,
                            key=key,
                            content=content,
                            content_type=content_type,
                            cache_control=cache_control,
                            etag=file_hash,  # Set ETag to file content hash
                        )
                    )
                else:
                    # For binary files, use source instead of content
                    files.append(
                        pulumi_aws.s3.BucketObject(
                            f"{self.name}-{key}-{file_hash[:8]}",  # Include hash in resource name
                            bucket=self.bucket.resources.bucket.id,
                            key=key,
                            source=pulumi.FileAsset(file_path),
                            cache_control=cache_control,
                            etag=file_hash,  # Set ETag to file content hash
                        )
                    )

        return S3StaticWebsiteResources(bucket=self.bucket, files=files)

