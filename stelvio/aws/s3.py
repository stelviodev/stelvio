from dataclasses import dataclass
from typing import final
import os
import hashlib
import mimetypes

import pulumi
import pulumi_aws
from pulumi import Output

from stelvio import context
from stelvio.aws.permission import AwsPermission
from stelvio.aws.acm import AcmValidatedDomain
from stelvio.aws.cloudfront import CloudFrontDistribution
from stelvio.component import Component, ComponentRegistry, link_config_creator
from stelvio.dns import Record
from stelvio.link import Link, Linkable, LinkConfig


@dataclass(frozen=True)
class S3BucketResources:
    bucket: pulumi_aws.s3.Bucket
    website_config: pulumi_aws.s3.BucketWebsiteConfiguration | None = None
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
        if self.resources.website_config:
            return self.resources.website_config.website_endpoint
        return pulumi.Output.concat("http://", self.resources.bucket.bucket, ".s3-website.", self.resources.bucket.region, ".amazonaws.com")

    def _create_resources(self) -> S3BucketResources:
        bucket = pulumi_aws.s3.Bucket(
            context().prefix(self.name),
            versioning={"enabled": self.versioning_enabled},
        )

        # Create website configuration separately
        website_config = pulumi_aws.s3.BucketWebsiteConfiguration(
            context().prefix(f"{self.name}-website"),
            bucket=bucket.id,
            index_document={"suffix": "index.html"},
            error_document={"key": "error.html"},
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
        pulumi.export(f"s3bucket_{self.name}_website_endpoint", website_config.website_endpoint)
        
        return S3BucketResources(bucket, website_config, cloudfront_distribution)

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
    # version_file: pulumi_aws.s3.BucketObject
    cloudfront_distribution: CloudFrontDistribution | None = None
    # record: Record | None = None
    acm_validated_domain: AcmValidatedDomain | None = None
    

@final
class S3StaticWebsite(Component[S3StaticWebsiteResources]):
    def __init__(self, name: str, directory: str, custom_domain: str):
        super().__init__(name)
        self.directory = directory
        self.custom_domain = custom_domain
        self._resources = None

    def _create_resources(self) -> S3StaticWebsiteResources:

        bucket = Bucket(
            # context().prefix(f"{self.name}-bucket"),
            f"{self.name}-bucket",
        )
        cloudfront_distribution = CloudFrontDistribution(
            name=f"{self.name}-cloudfront",
            s3_bucket=bucket.resources.bucket,
            custom_domain=self.custom_domain,
        )
        # record = context().dns.create_record(
        #     resource_name=f"s3staticwebsite_{self.name}-custom-domain-record",
        #     name=self.custom_domain,
        #     record_type="CNAME",
        #     value=cloudfront_distribution.domain_name,
        # )


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
                
                # Create a more robust resource name
                import re
                # Convert path separators and special chars to dashes, ensure valid Pulumi resource name
                safe_key = re.sub(r'[^a-zA-Z0-9]', '-', key)
                # Remove consecutive dashes and leading/trailing dashes
                safe_key = re.sub(r'-+', '-', safe_key).strip('-')
                # resource_name = f"{self.name}-{safe_key}-{file_hash[:8]}"

                # DO NOT INCLUDE HASH IN RESOURCE NAME
                # If the resource name changes, Pulumi will treat it as a new resource, and create a new s3 object
                # Then, the old one is deleted by pulumi. Sounds correct, but since the filename (key) is the same, the delete operation deletes the new object!
                resource_name = f"{self.name}-{safe_key}"

                # For binary files, use source instead of content
                mimetype, _ = mimetypes.guess_type(filename)
                
                # Ensure proper MIME types for common web files
                if mimetype is None:
                    if filename.endswith('.css'):
                        mimetype = 'text/css'
                    elif filename.endswith('.js'):
                        mimetype = 'application/javascript'
                    elif filename.endswith('.html'):
                        mimetype = 'text/html'
                    elif filename.endswith('.json'):
                        mimetype = 'application/json'
                    elif filename.endswith('.xml'):
                        mimetype = 'application/xml'
                    elif filename.endswith('.svg'):
                        mimetype = 'image/svg+xml'
                    else:
                        mimetype = 'application/octet-stream'
                
                # Set appropriate cache control based on file type
                if filename.endswith(('.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg', '.woff', '.woff2', '.ttf', '.eot')):
                    # Static assets can be cached longer
                    cache_control = "public, max-age=31536000"  # 1 year
                elif filename.endswith(('.html', '.json', '.xml')):
                    # Dynamic content should have shorter cache
                    cache_control = "public, max-age=300"  # 5 minutes
                else:
                    # Default short cache
                    cache_control = "public, max-age=1"  # 1 second
                
                try:
                    bucket_object = pulumi_aws.s3.BucketObject(
                        resource_name,
                        bucket=bucket.resources.bucket.id,
                        key=key,
                        source=pulumi.FileAsset(file_path),
                        content_type=mimetype,
                        cache_control=cache_control,
                        opts=pulumi.ResourceOptions(
                            depends_on=[bucket.resources.bucket]  # Ensure bucket exists before creating objects
                        )
                    )
                    files.append(bucket_object)
                except Exception as e:
                    print(f"Error creating bucket object for {key}: {e}")
                    raise
        
        # # Summary output for verification
        # print(f"Successfully processed {len(files)} files for S3 upload")

        # # Add a resource that outputs invalidation instructions when content changes
        # if self.bucket.resources.cloudfront_distribution:
        #     # Calculate overall content hash for invalidation tracking
        #     content_hash = hashlib.md5()
        #     for root, _, filenames in os.walk(self.directory):
        #         for filename in filenames:
        #             file_path = os.path.join(root, filename)
        #             with open(file_path, 'rb') as f:
        #                 content_hash.update(f.read())
            
        #     # Create a version file that changes when content changes

            
            # Export invalidation information for manual or automated use
            # pulumi.export(f"s3_static_website_{self.name}_content_hash", content_hash.hexdigest())


            # pulumi.export(f"s3_static_website_{self.name}_cloudfront_distribution_id", 
            #              self.bucket.resources.cloudfront_distribution.resources.distribution.id)
            


            # pulumi.export(f"s3_static_website_{self.name}_invalidation_command", 
            #              pulumi.Output.concat(
            #                  "aws cloudfront create-invalidation --distribution-id ",
            #                  self.bucket.resources.cloudfront_distribution.resources.distribution.id,
            #                  " --paths '/*'"
            #              ))
        # else:
        #     # Create a simple version file even without CloudFront
        #     version_file = pulumi_aws.s3.BucketObject(
        #         f"{self.name}-version-simple",
        #         bucket=self.bucket.resources.bucket.id,
        #         key="version.json", 
        #         content='{"version": "no-cloudfront"}',
        #         content_type="application/json",
        #     )

        pulumi.export(f"s3_static_website_{self.name}_bucket_name", bucket.resources.bucket.bucket)
        pulumi.export(f"s3_static_website_{self.name}_bucket_arn", bucket.resources.bucket.arn)
        pulumi.export(f"s3_static_website_{self.name}_website_url", bucket.website_url)
        pulumi.export(f"s3_static_website_{self.name}_cloudfront_distribution_name", cloudfront_distribution.name)
        pulumi.export(f"s3_static_website_{self.name}_cloudfront_domain_name", cloudfront_distribution.resources.distribution.domain_name)
        pulumi.export(f"s3_static_website_{self.name}_custom_domain", self.custom_domain)
        pulumi.export(f"s3_static_website_{self.name}_files", [
            file.arn for file in files
        ])
        # pulumi.export(f"s3_static_website_{self.name}_cloudfront_distribution_id", cloudfront_distribution.resources.distribution.id)

        # return S3StaticWebsiteResources(bucket=self.bucket, files=files, version_file=version_file,)
        return S3StaticWebsiteResources(
            bucket=bucket,
            files=files,
            cloudfront_distribution=cloudfront_distribution,
            # record=record,
        )

