"""Disposable normal-CLI policy acceptance; requires an explicit proof UUID."""

import os
from uuid import UUID

import pulumi

from stelvio.app import StelvioApp
from stelvio.aws.vpc import BastionConfig, Vpc
from stelvio.config import AwsConfig, StelvioAppConfig

owner = str(UUID(os.environ["STLV_POLICY_PROOF_OWNER"]))
batch = int(os.environ["STLV_POLICY_PROOF_BATCH"])
if batch not in (0, 1):
    raise ValueError("Policy proof batch must be 0 or 1")
app = StelvioApp(f"g2-policy-{UUID(owner).hex[:16]}-{batch}")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(profile="default", region="us-east-1"),
        environments=["proof"],
        tags={"stlv:g2-policy-owner": owner},
    )


@app.run
def run() -> None:
    forms = (
        {"omitted": None, "true": True, "false": False}
        if batch == 0
        else {
            "config": BastionConfig(),
            "dict": {"dns_domains": [f"policy-{UUID(owner).hex}.invalid"]},
            "empty": {},
        }
    )
    for name, policy in forms.items():
        network = Vpc(name, az=1) if name == "omitted" else Vpc(name, az=1, bastion=policy)
        pulumi.export(name, network.resources.vpc.id)
