# Ruff: proof assertions and JSON stdout are the executable acceptance interface.
# ruff: noqa: S101, T201
"""Audit recorded P6 owners; optionally purge their exact, certified-empty metadata."""

import argparse
import json
import os
import re
import sys
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from stelvio.tunnel.credentials import AWS_IO
from tests.integration.assert_helpers import _boto3_session
from tests.integration.assert_tunnel import assert_access_absent, assert_application_absent
from tests.integration.tunnel_cleanup import purge_test_metadata

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("runs", nargs="+")
parser.add_argument("--purge", action="store_true")
arguments = parser.parse_args()
os.environ["STLV_TEST_AWS_PROFILE"] = "default"
os.environ["STLV_TEST_AWS_REGION"] = "us-east-1"
sdk = _boto3_session()
account = sdk.client("sts", config=AWS_IO).get_caller_identity()["Account"]
assert account == "535368238919"
base = Path(__file__).parent / "build/p6"
historical = {
    "565077": "7514c078-4667-469c-a132-cbff037c779f",
    "6a8133": "dbedc682-f1cd-4b42-935f-024997f585a8",
    "7bdc9d": "3a838f43-d5b3-4091-bfc1-43b8d221b23c",
}
results = []
for run in arguments.runs:
    assert re.fullmatch(r"[0-9a-f]{6}", run)
    owner = json.loads((base / run / "ownership.json").read_text())
    assert owner["app"] == f"stlv-{run}"
    assert owner["env"] == "test"
    assert owner["account"] == account
    assert owner["region"] == sdk.region_name
    assert owner["profile"] == "default"
    assert_application_absent(owner["app"], owner["env"], include_lambda_logs=not arguments.purge)
    sessions = {historical[run]} if run in historical else set()
    owner_hashes = {sha256(s.encode()).hexdigest()[:16] for s in sessions}
    for path in (base / run).glob("resolved-ownership*.json"):
        saved = json.loads(path.read_text())
        sessions.update(i["session"] for i in saved["intents"])
        owner_hashes.update(i["owner_hash"] for i in saved.get("transports", []))
    owner_hashes.update(sha256(s.encode()).hexdigest()[:16] for s in sessions)
    assert_access_absent(sdk, sessions, owner_hashes)
    ec2 = sdk.client("ec2", config=AWS_IO)
    filters = [{"Name": "tag:stelvio:app", "Values": [owner["app"]]}]
    assert not ec2.describe_addresses(Filters=filters)["Addresses"]
    secret_prefix = f"{owner['app']}-{owner['env']}-"
    # AWS deletes the managed password with its cluster; scheduled service
    # tombstones, like terminated EC2 records, are not active billable resources.
    assert not [
        secret["ARN"]
        for page in sdk.client("secretsmanager", config=AWS_IO)
        .get_paginator("list_secrets")
        .paginate(IncludePlannedDeletion=True)
        for secret in page["SecretList"]
        if not secret.get("DeletedDate")
        and any(
            t["Key"].startswith("aws:") and (":cluster:" + secret_prefix) in t["Value"]
            for t in secret.get("Tags", [])
        )
    ]
    result = {"run": run, "aws_absent": True}
    if arguments.purge:
        result.update(
            purge_test_metadata(
                sdk, app=owner["app"], env=owner["env"], bucket=owner["bucket"], account=account
            )
        )
    results.append(result)
    (base / run / "cleanup-audit.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result), flush=True)
