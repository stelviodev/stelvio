from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    CredentialRetrievalError,
    NoCredentialsError,
    PartialCredentialsError,
    ProfileNotFound,
    SSOError,
    TokenRetrievalError,
)

from stelvio.exceptions import StelvioValidationError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

# botocore errors a user can fix by configuring credentials; anything else keeps its traceback.
_SETUP_ERRORS = (
    ProfileNotFound,
    NoCredentialsError,
    PartialCredentialsError,
    CredentialRetrievalError,
    SSOError,
    TokenRetrievalError,
)
# SSM's spellings; it is the only service behind convert_aws_errors.
_DENIED_CODE = "AccessDeniedException"
_AUTH_CODES = frozenset(
    {
        _DENIED_CODE,
        "ExpiredTokenException",  # SSO / STS session ran out
        "UnrecognizedClientException",  # access key id does not exist
        "InvalidSignatureException",  # secret key is wrong
    }
)
# botocore's order: AWS_DEFAULT_PROFILE wins over AWS_PROFILE.
_PROFILE_VARS = ("AWS_DEFAULT_PROFILE", "AWS_PROFILE")


@contextmanager
def convert_aws_errors(profile: str | None, region: str | None) -> Iterator[None]:
    """Turn credential and permission failures into a StelvioValidationError with a fix hint.

    Other AWS errors (throttling, missing bucket, unreachable endpoint, ...) propagate
    unchanged so their traceback stays useful. STLV_DEBUG=1 disables the conversion.
    """
    try:
        yield
    except (BotoCoreError, ClientError) as e:
        code = e.response["Error"]["Code"] if isinstance(e, ClientError) else None
        translatable = isinstance(e, _SETUP_ERRORS) or code in _AUTH_CODES
        if not translatable or os.getenv("STLV_DEBUG") == "1":
            raise
        region_part = f", region: {region}" if region else ""
        raise StelvioValidationError(
            f"{e}\n  Profile: {_describe_profile(profile)}{region_part}\n  {_fix(profile, code)}"
        ) from e


def _env_profile() -> tuple[str | None, str | None]:
    """(name, variable) of the profile botocore takes from the environment, else (None, None)."""
    for var in _PROFILE_VARS:
        if var in os.environ:
            return os.environ[var], var
    return None, None


def _describe_profile(profile: str | None) -> str:
    """Which profile was used and where it came from; botocore's own message never says."""
    if profile is not None:
        return f"'{profile}' (AwsConfig in stlv_app.py)"
    name, var = _env_profile()
    if var is None:
        return "default (AWS_PROFILE not set)"
    if name == "":
        return f"'' ({var} is set but empty)"
    return f"'{name}' ({var})"


def _fix(profile: str | None, code: str | None) -> str:
    if code == _DENIED_CODE:
        return (
            "Stelvio needs SSM Parameter Store (/stlv/*) and S3 (stlv-state-* bucket) "
            "access for its state home."
        )
    name = profile if profile is not None else _env_profile()[0]
    login = f"aws sso login --profile {name}" if name else "aws sso login"
    return (
        f"Check your AWS setup: 'aws configure', '{login}', AWS_PROFILE, "
        "or AwsConfig(profile=...) in stlv_app.py."
    )


class AwsHome:
    """AWS implementation of Home - S3 for files, SSM for params."""

    def __init__(self, profile: str | None = None, region: str | None = None) -> None:
        self._profile = profile
        with convert_aws_errors(profile, region):
            self._session = boto3.Session(profile_name=profile, region_name=region)
            self._ssm = self._session.client("ssm")
            self._s3 = self._session.client("s3")
        self._bucket: str | None = None

    def read_param(self, name: str) -> str | None:
        # First AWS API call of every stlv command, so credential problems surface here.
        # Later calls reuse the same credentials; a partial IAM policy (SSM allowed, S3
        # denied) still gets a raw traceback.
        with convert_aws_errors(self._profile, self._session.region_name):
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
