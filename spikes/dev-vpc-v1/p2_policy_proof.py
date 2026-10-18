"""Normal deploy/destroy policy matrix; retains logs/intent and removes owned AWS metadata."""

import argparse
import json
import os
import subprocess
from pathlib import Path
from uuid import UUID

import boto3
from botocore.exceptions import ClientError


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "recover"))
    parser.add_argument("--owner", required=True)
    parser.add_argument("--batch", type=int, choices=(0, 1), required=True)
    args = parser.parse_args()
    owner = str(UUID(args.owner))
    os.umask(0o077)
    directory = Path(__file__).parent
    root = directory / "build/policy" / owner / str(args.batch)
    root.mkdir(parents=True, exist_ok=True)
    app = f"g2-policy-{UUID(owner).hex[:16]}-{args.batch}"
    intent = root / "intent.json"
    expected = {
        "owner": owner,
        "app": app,
        "env": "proof",
        "account": "535368238919",
        "region": "us-east-1",
    }
    if intent.exists():
        assert json.loads(intent.read_text()) == expected
        if args.command == "run":
            raise RuntimeError("Recover existing intent before fresh UUID")
    else:
        with intent.open("x") as stream:
            json.dump(expected, stream)
            stream.flush()
            os.fsync(stream.fileno())
    session = boto3.Session(profile_name="default", region_name="us-east-1")
    assert session.client("sts").get_caller_identity()["Account"] == expected["account"]
    ec2 = session.client("ec2")
    environment = dict(
        os.environ, STLV_POLICY_PROOF_OWNER=owner, STLV_POLICY_PROOF_BATCH=str(args.batch)
    )
    cli = directory.parents[1] / ".venv/bin/stlv"

    def command(verb):
        with (root / (verb + ".log")).open("w") as log:
            child = subprocess.Popen(
                [str(cli), verb, "proof", "--yes", "--json"],
                cwd=directory / "p2-policy-app",
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            # Do not kill a creator with potentially detached providers. Retain
            # its state/log path if it exceeds the acceptance budget.
            code = child.wait(timeout=900)
        if code:
            raise RuntimeError(f"Normal CLI {verb} failed; inspect {root / (verb + '.log')}")
        print(json.dumps({"phase": verb, "app": app}), flush=True)

    def owned_nodes():
        return [
            n
            for r in ec2.describe_instances(
                Filters=[
                    {"Name": "tag:stlv:g2-policy-owner", "Values": [owner]},
                    {"Name": "tag:stelvio:app", "Values": [app]},
                ]
            )["Reservations"]
            for n in r["Instances"]
            if n["State"]["Name"] != "terminated"
        ]

    try:
        if args.command == "run":
            command("deploy")
            groups = ec2.describe_security_groups(
                Filters=[
                    {"Name": "tag:stlv:g2-policy-owner", "Values": [owner]},
                    {"Name": "tag:stelvio:app", "Values": [app]},
                ]
            )["SecurityGroups"]
            nodes = owned_nodes()
            assert len(nodes) == (1 if not args.batch else 3)
            assert all(not g["IpPermissions"] for g in groups)
            assert all(
                not any(t["Key"].startswith("stlv:tunnel-") for t in n.get("Tags", []))
                for n in nodes
            )
            # Ordinary diff must retain persistent access without replacement.
            with (root / "diff.log").open("w") as log:
                result = subprocess.run(
                    [str(cli), "diff", "proof", "--json"],
                    cwd=directory / "p2-policy-app",
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=300,
                    check=False,
                )
            assert not result.returncode
            assert {n["InstanceId"] for n in owned_nodes()} == {n["InstanceId"] for n in nodes}
            print(
                json.dumps(
                    {
                        "phase": "PASS",
                        "batch": args.batch,
                        "persistent_instances": len(nodes),
                        "no_public_ssh": True,
                        "ordinary_deploy_no_temporary_access": True,
                    }
                ),
                flush=True,
            )
    finally:
        command("destroy")
        assert not owned_nodes()
        assert not ec2.describe_vpcs(
            Filters=[
                {"Name": "tag:stlv:g2-policy-owner", "Values": [owner]},
                {"Name": "tag:stelvio:app", "Values": [app]},
            ]
        )["Vpcs"]
        # The bucket/bootstrap are baseline. Purge only this UUID application's
        # own state/update/snapshot/lock/eventlog versions and dedicated key.
        s3 = session.client("s3")
        for prefix in (
            f"state/{app}/proof.json",
            f"lock/{app}/proof.json",
            *(f"{kind}/{app}/proof/" for kind in ("snapshot", "update", "eventlog")),
        ):
            versions = [
                {"Key": v["Key"], "VersionId": v["VersionId"]}
                for page in s3.get_paginator("list_object_versions").paginate(
                    Bucket="stlv-state-c054b7aca79c",
                    Prefix=prefix,
                    ExpectedBucketOwner=expected["account"],
                )
                for v in page.get("Versions", []) + page.get("DeleteMarkers", [])
            ]
            for offset in range(0, len(versions), 1000):
                response = s3.delete_objects(
                    Bucket="stlv-state-c054b7aca79c",
                    ExpectedBucketOwner=expected["account"],
                    Delete={"Objects": versions[offset : offset + 1000], "Quiet": True},
                )
                assert not response.get("Errors")
        ssm = session.client("ssm")
        try:
            ssm.delete_parameter(Name=f"/stlv/passphrase/{app}/proof")
        except ClientError as error:
            if error.response["Error"]["Code"] != "ParameterNotFound":
                raise
        print(json.dumps({"phase": "fixture-removed", "app": app}), flush=True)


if __name__ == "__main__":
    main()
