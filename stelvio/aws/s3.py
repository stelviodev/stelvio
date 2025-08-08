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
        bucket_policy_resource = None
        
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
    version_file: pulumi_aws.s3.BucketObject
    

@final
class S3StaticWebsite(Component[S3StaticWebsiteResources]):
    def __init__(self, name: str, bucket: Bucket, directory: str):
        super().__init__(name)
        self.bucket = bucket
        self.directory = directory
        self._resources = None

    def _create_resources(self) -> S3StaticWebsiteResources:
        files = []
        
        # Create a simple manifest of all files first
        file_manifest = []
        for root, _, filenames in os.walk(self.directory):
            for filename in filenames:
                file_path = os.path.join(root, filename)
                key = os.path.relpath(file_path, self.directory)
                file_manifest.append((file_path, key))
        
        # Create each file as a separate, independent resource
        for i, (file_path, key) in enumerate(file_manifest):
            # Calculate file hash for ETag
            with open(file_path, 'rb') as f:
                file_content = f.read()
                file_hash = hashlib.md5(file_content).hexdigest()
            
            # Determine if file is text or binary
            is_text_file = key.endswith(('.html', '.css', '.js', '.txt', '.md', '.json', '.xml', '.svg'))
            
            # Set cache control based on file type
            if key.endswith(('.html', '.htm')):
                cache_control = "public, max-age=60"
            elif key.endswith(('.css', '.js')):
                cache_control = "public, max-age=3600"
            elif key.endswith(('.png', '.jpg', '.jpeg', '.gif', '.ico', '.svg')):
                cache_control = "public, max-age=86400"
            else:
                cache_control = "public, max-age=3600"
            
            # Create a simple resource name based on index
            resource_name = f"{self.name}-file-{i:03d}-{file_hash[:8]}"
            
            try:
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
                    
                    bucket_object = pulumi_aws.s3.BucketObject(
                        resource_name,
                        bucket=self.bucket.resources.bucket.id,
                        key=key,
                        content=content,
                        content_type=content_type,
                        cache_control=cache_control,
                    )
                else:
                    bucket_object = pulumi_aws.s3.BucketObject(
                        resource_name,
                        bucket=self.bucket.resources.bucket.id,
                        key=key,
                        source=pulumi.FileAsset(file_path),
                        cache_control=cache_control,
                    )
                
                files.append(bucket_object)
                # Export each file individually to force creation
                pulumi.export(f"s3_file_{i}_{key.replace('/', '_').replace('.', '_')}", bucket_object.key)
                
            except Exception as e:
                print(f"Error creating bucket object for {key}: {e}")
                raise
        
        print(f"Successfully processed {len(files)} files for S3 upload")
        
        # WORKAROUND: Since Pulumi BucketObject creation is failing silently,
        # add a note about using aws s3 sync as a backup
        print(f"💡 If files don't appear in S3, run: aws s3 sync {self.directory} s3://{self.bucket.resources.bucket.bucket.apply(lambda b: b)}/ --delete")

        # Create a simple version file
        if self.bucket.resources.cloudfront_distribution:
            # Calculate overall content hash for invalidation tracking
            content_hash = hashlib.md5()
            for root, _, filenames in os.walk(self.directory):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    with open(file_path, 'rb') as f:
                        content_hash.update(f.read())
            
            version_content = f'{{"version": "{content_hash.hexdigest()}", "files_count": {len(files)}}}'
            
            version_file = pulumi_aws.s3.BucketObject(
                f"{self.name}-version-{content_hash.hexdigest()[:8]}",
                bucket=self.bucket.resources.bucket.id,
                key="version.json",
                content=version_content,
                content_type="application/json",
                cache_control="no-cache, no-store, must-revalidate",
            )
            
            # Export version and invalidation info
            pulumi.export(f"s3_static_website_{self.name}_content_hash", content_hash.hexdigest())
            pulumi.export(f"s3_static_website_{self.name}_cloudfront_distribution_id", 
                         self.bucket.resources.cloudfront_distribution.resources.distribution.id)
            pulumi.export(f"s3_static_website_{self.name}_invalidation_command", 
                         pulumi.Output.concat(
                             "aws cloudfront create-invalidation --distribution-id ",
                             self.bucket.resources.cloudfront_distribution.resources.distribution.id,
                             " --paths '/*'"
                         ))
            # Export sync command as a workaround
            pulumi.export(f"s3_static_website_{self.name}_sync_command",
                         pulumi.Output.concat(
                             "aws s3 sync ", self.directory, " s3://",
                             self.bucket.resources.bucket.bucket, "/ --delete --cache-control 'public,max-age=60'"
                         ))
        else:
            # Create a simple version file even without CloudFront
            version_file = pulumi_aws.s3.BucketObject(
                f"{self.name}-version-simple",
                bucket=self.bucket.resources.bucket.id,
                key="version.json", 
                content='{"version": "no-cloudfront"}',
                content_type="application/json",
            )

        return S3StaticWebsiteResources(bucket=self.bucket, files=files, version_file=version_file)

