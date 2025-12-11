import hashlib
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


class AwsHome:
    """AWS implementation of Home - S3 for files, SSM for params."""

    def __init__(self, profile: str | None = None, region: str | None = None) -> None:
        self._session = boto3.Session(profile_name=profile, region_name=region)
        self._ssm = self._session.client("ssm")
        self._s3 = self._session.client("s3")
        self._bucket: str | None = None

    def read_param(self, name: str) -> str | None:
        try:
            response = self._ssm.get_parameter(Name=name, WithDecryption=True)
            return response["Parameter"]["Value"]
        except ClientError as e:
            if e.response["Error"]["Code"] == "ParameterNotFound":
                return None
            raise

    def write_param(
        self, name: str, value: str, description: str = "", *, secure: bool = False
    ) -> None:
        self._ssm.put_parameter(
            Name=name,
            Value=value,
            Type="SecureString" if secure else "String",
            Description=description,
            Overwrite=True,
        )

    def init_storage(self, name: str | None = None) -> str:
        """Initialize S3 bucket. If name is None, generate and create. Returns bucket name."""
        if name is None:
            name = self._generate_bucket_name()
            self._create_bucket(name)
        self._bucket = name
        return name

    def _generate_bucket_name(self) -> str:
        """Generate bucket name from account ID and region."""
        account_id = self._session.client("sts").get_caller_identity()["Account"]
        region = self._session.region_name
        hash_input = f"{account_id}{region}".encode()
        hash_suffix = hashlib.sha256(hash_input).hexdigest()[:12]
        return f"stlv-state-{hash_suffix}"

    def _create_bucket(self, name: str) -> None:
        """Create S3 bucket with versioning enabled."""
        region = self._session.region_name
        if region == "us-east-1":
            self._s3.create_bucket(Bucket=name)
        else:
            self._s3.create_bucket(
                Bucket=name,
                CreateBucketConfiguration={"LocationConstraint": region},
            )
        self._s3.put_bucket_versioning(
            Bucket=name,
            VersioningConfiguration={"Status": "Enabled"},
        )

    def read_file(self, key: str, local_path: Path) -> bool:
        """Download file from S3. Returns True if file existed."""
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._s3.download_file(self._bucket, key, str(local_path))
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise
        else:
            return True

    def write_file(self, key: str, local_path: Path) -> None:
        """Upload file to S3."""
        self._s3.upload_file(str(local_path), self._bucket, key)

    def delete_file(self, key: str) -> None:
        """Delete file from S3."""
        self._s3.delete_object(Bucket=self._bucket, Key=key)

    def file_exists(self, key: str) -> bool:
        """Check if file exists in S3."""
        try:
            self._s3.head_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] == "404":
                return False
            raise
        else:
            return True

    def delete_prefix(self, prefix: str) -> None:
        """Delete all files with given prefix."""
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                self._s3.delete_object(Bucket=self._bucket, Key=obj["Key"])
