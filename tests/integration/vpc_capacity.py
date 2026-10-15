"""Choose integration workers from the account's remaining regional VPC capacity."""

import os
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

MAX_VPC_WORKERS = 4
VPC_QUOTA_CODE = "L-F678F1CE"


def main() -> int:
    """Print a worker count for run_all.sh, or fail before launching tests."""
    profile = os.environ.get("STLV_TEST_AWS_PROFILE") or None
    profile_label = profile or "default"
    region = os.environ.get("STLV_TEST_AWS_REGION", "us-east-1")

    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        quotas = session.client("service-quotas")
        quota = int(
            quotas.get_service_quota(ServiceCode="vpc", QuotaCode=VPC_QUOTA_CODE)["Quota"]["Value"]
        )
        ec2 = session.client("ec2")
        existing = sum(
            len(page.get("Vpcs", [])) for page in ec2.get_paginator("describe_vpcs").paginate()
        )
    except (BotoCoreError, ClientError, KeyError, TypeError, ValueError) as error:
        print(
            f"VPC preflight failed for profile {profile_label!r} in {region}: {error}. "
            "Check AWS credentials and access to ec2:DescribeVpcs and "
            "servicequotas:GetServiceQuota before rerunning.",
            file=sys.stderr,
        )
        return 1

    workers = min(MAX_VPC_WORKERS, quota - existing)
    if workers <= 0:
        print(
            f"No VPC capacity available in {region}: quota {quota}, existing {existing}. "
            "Clean up verified orphan integration stacks or increase the regional VPC quota "
            "before rerunning. No tests were launched.",
            file=sys.stderr,
        )
        return 1

    print(
        f"VPC tier in {region}: quota {quota}, existing {existing}, using {workers} workers.",
        file=sys.stderr,
    )
    print(workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
