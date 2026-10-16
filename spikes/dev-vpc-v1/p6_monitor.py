# Proof assertions and JSON stdout are the executable acceptance interface.
# ruff: noqa: S101, T201
"""Read-only progress for a recorded P6 CLI owner (never print credential inputs)."""

import json
import os
import re
import sys
from pathlib import Path

from botocore.exceptions import ClientError

os.environ["STLV_TEST_AWS_PROFILE"] = "default"
os.environ["STLV_TEST_AWS_REGION"] = "us-east-1"
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from stelvio.tunnel.credentials import AWS_IO
from stelvio.tunnel.helper_client import HelperError, NativeHelper
from tests.integration.assert_helpers import _boto3_session

run = sys.argv[1]
assert re.fullmatch(r"[0-9a-f]{6}", run)
owner = json.loads((Path(__file__).parent / "build/p6" / run / "ownership.json").read_text())
s = _boto3_session()
assert s.client("sts", config=AWS_IO).get_caller_identity()["Account"] == owner["account"]
result = {"run": run}
try:
    response = s.client("s3", config=AWS_IO).get_object(
        Bucket=owner["bucket"],
        ExpectedBucketOwner=owner["account"],
        Key=f"state/{owner['app']}/{owner['env']}.json",
    )
    with response["Body"] as body:
        state = json.loads(body.read())
    resources = state.get("checkpoint", state).get("latest", state).get("resources", [])
    result["state_resources"] = len(resources)
    pending = state.get("checkpoint", state).get("latest", state).get("pending_operations", [])
    result["pending"] = len(pending)
    result["pending_resources"] = [
        {"operation": item.get("type"), "resource": item.get("resource", {}).get("type")}
        for item in pending
    ]
except ClientError as e:
    result["state_error"] = e.response["Error"]["Code"]
result["clusters"] = [
    {"id": c["DBClusterIdentifier"], "status": c["Status"]}
    for c in s.client("docdb", config=AWS_IO).describe_db_clusters()["DBClusters"]
    if c["DBClusterIdentifier"].startswith(f"{owner['app']}-{owner['env']}-")
]
result["vpcs"] = [
    v["VpcId"]
    for v in s.client("ec2", config=AWS_IO).describe_vpcs(
        Filters=[{"Name": "tag:stelvio:app", "Values": [owner["app"]]}]
    )["Vpcs"]
]
try:
    i = NativeHelper().inspect()
except (HelperError, OSError) as error:
    # A foreign active peer is refused; this does not certify an empty host.
    result["host"] = {"inspection_unavailable": type(error).__name__}
else:
    result["host"] = {"owned": i.owned, "uncertain": i.uncertain, "units": len(i.units)}
print(json.dumps(result))
