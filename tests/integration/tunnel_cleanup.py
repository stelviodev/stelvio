"""Remove exact disposable CLI-test metadata only after certified AWS teardown."""

import json
import re
from base64 import urlsafe_b64encode

from botocore.exceptions import ClientError

from stelvio.tunnel.credentials import AWS_IO

from .assert_tunnel import assert_application_absent


def purge_test_metadata(sdk, *, app: str, env: str, bucket: str, account: str) -> dict:
    assert re.fullmatch(r"stlv-[0-9a-f]{6}", app)
    assert env == "test"
    assert sdk.client("sts", config=AWS_IO).get_caller_identity()["Account"] == account
    s3 = sdk.client("s3", config=AWS_IO)
    state_key = f"state/{app}/{env}.json"
    try:
        response = s3.get_object(Bucket=bucket, ExpectedBucketOwner=account, Key=state_key)
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchKey":
            raise
    else:
        with response["Body"] as body:
            state = json.loads(body.read())["checkpoint"]["latest"]
        assert not state.get("resources")
        assert not state.get("pending_operations")
    assert_application_absent(app, env, include_lambda_logs=False, sdk=sdk)
    segments = [urlsafe_b64encode(s.encode()).decode().rstrip("=") for s in (app, env)]
    pages = s3.get_paginator("list_object_versions").paginate(
        Bucket=bucket, ExpectedBucketOwner=account, Prefix=f"tunnel/{'/'.join(segments)}/"
    )
    assert not any(p.get("Versions") or p.get("DeleteMarkers") for p in pages)
    log_groups = _remove_lambda_logs(sdk, app, env)
    assert_application_absent(app, env, sdk=sdk)
    prefixes = [
        state_key,
        f"lock/{app}/{env}.json",
        *[f"{kind}/{app}/{env}/" for kind in ("snapshot", "update", "eventlog")],
    ]
    removed = 0
    for prefix in prefixes:
        versions = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for page in s3.get_paginator("list_object_versions").paginate(
                Bucket=bucket, ExpectedBucketOwner=account, Prefix=prefix
            )
            for item in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        assert all(
            item["Key"].startswith(prefix) if prefix.endswith("/") else item["Key"] == prefix
            for item in versions
        )
        for start in range(0, len(versions), 1000):
            result = s3.delete_objects(
                Bucket=bucket,
                ExpectedBucketOwner=account,
                Delete={"Objects": versions[start : start + 1000], "Quiet": True},
            )
            assert not result.get("Errors")
        remaining = s3.get_paginator("list_object_versions").paginate(
            Bucket=bucket, ExpectedBucketOwner=account, Prefix=prefix
        )
        assert not any(p.get("Versions") or p.get("DeleteMarkers") for p in remaining)
        removed += len(versions)
    _remove_passphrase(sdk, app, env)
    return {
        "app": app,
        "aws_absent": True,
        "removed_versions": removed,
        "removed_log_groups": log_groups,
        "passphrase_absent": True,
    }


def _remove_lambda_logs(sdk, app: str, env: str) -> int:
    """Lambda-created logs survive function deletion; own only the exact test prefix."""
    prefix = f"/aws/lambda/{app}-{env}-"
    logs = sdk.client("logs", config=AWS_IO)
    names = [
        group["logGroupName"]
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix)
        for group in page.get("logGroups", [])
    ]
    assert all(re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]+", name) for name in names)
    for name in names:
        try:
            logs.delete_log_group(logGroupName=name)
        except ClientError as error:
            if error.response["Error"]["Code"] != "ResourceNotFoundException":
                raise
    assert not any(
        page.get("logGroups")
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix)
    )
    return len(names)


def _remove_passphrase(sdk, app: str, env: str) -> None:
    ssm = sdk.client("ssm", config=AWS_IO)
    parameter = f"/stlv/passphrase/{app}/{env}"
    try:
        ssm.delete_parameter(Name=parameter)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ParameterNotFound":
            raise
    try:
        ssm.get_parameter(Name=parameter)
    except ClientError as error:
        if error.response["Error"]["Code"] != "ParameterNotFound":
            raise
    else:
        raise AssertionError("Proof passphrase remains")
