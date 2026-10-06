"""The independent access program must never recreate application resources."""

import json
from dataclasses import replace
from unittest.mock import Mock

import pulumi
from pytest import raises

from stelvio.component import resource_name
from stelvio.tunnel.access_program import CapturedCredentials, access_program
from stelvio.tunnel.access_state import AccessIntent, AccessJournal, AccessTarget
from tests.aws.pulumi_mocks import R
from tests.tunnel.storage import Storage


def planned_journal():
    journal = Mock()
    journal.intent = AccessIntent(
        session="10e08906-0762-4410-9963-2c56a2e06400",
        unit="21f7a531",
        app="app",
        environment="dev",
        owner_uid=502,
        account="123456789012",
        region="us-east-1",
        vpc_id="vpc-application",
        subnet_id="subnet-application",
        availability_zone="us-east-1a",
        ami="ami-captured",
        targets=(AccessTarget("sg-database", 27018), AccessTarget("sg-custom", 27017)),
    )
    return journal


def test_temporary_program_owns_only_access_and_exact_service_ingress(pulumi_mocks):
    journal = planned_journal()
    credentials = CapturedCredentials("123456789012", "us-east-1", "test-key", "test-secret", None)

    @pulumi.runtime.test
    def deploy():
        access_program(journal, credentials)

    deploy()
    journal.require_claim.assert_called_once_with()
    pulumi_mocks.assert_res(
        "tunnel-aws",
        R.AWS_PROVIDER,
        {
            "region": "us-east-1",
            "allowedAccountIds": '["123456789012"]',
            "skipCredentialsValidation": "false",
            "skipRegionValidation": "true",
        },
        prefixed=False,
    )
    intent = journal.intent
    group = pulumi_mocks.assert_res(
        resource_name(intent.name, suffix="-bastion-sg", limit=255),
        R.SECURITY_GROUP,
        {
            "name": intent.name + "-sg",
            "vpcId": "vpc-application",
            "description": "Stelvio SSM access: no inbound SSH",
            "tags": intent.tags,
        },
        prefixed=False,
    )
    instance = pulumi_mocks.assert_res(
        resource_name(intent.name, suffix="-bastion", limit=64), R.EC2_INSTANCE, prefixed=False
    )
    assert instance.inputs["ami"] == "ami-captured"
    assert instance.inputs["subnetId"] == "subnet-application"
    assert instance.inputs["vpcSecurityGroupIds"] == [group.name + "-test-id"]
    assert instance.inputs["availabilityZone"] == "us-east-1a"
    assert instance.inputs["tags"] == instance.inputs["volumeTags"] == intent.tags
    assert instance.inputs.get("keyName") is None
    for index, target in enumerate(intent.targets):
        # These root resources have no app/environment prefix: they belong to
        # the independently configured access backend, not a VPC component.
        pulumi_mocks.assert_res(
            f"service-access-{index}",
            R.SECURITY_GROUP_INGRESS_RULE,
            {
                "securityGroupId": target.security_group_id,
                "referencedSecurityGroupId": group.name + "-test-id",
                "ipProtocol": "tcp",
                "fromPort": target.port,
                "toPort": target.port,
                "tags": intent.tags,
            },
            prefixed=False,
        )
    for typ, suffix in (
        (R.ROLE, "-role"),
        (R.INSTANCE_PROFILE, "-profile"),
        (R.SSM_DOCUMENT, "-identity"),
    ):
        resource = pulumi_mocks.assert_res(
            resource_name(
                intent.name, suffix="-bastion" + suffix, limit=64 if typ == R.ROLE else 128
            ),
            typ,
            prefixed=False,
        )
        assert resource.inputs["name"] == intent.name + suffix
        assert resource.inputs["tags"] == intent.tags
    pulumi_mocks.assert_res_counts(
        {
            R.SECURITY_GROUP: 1,
            R.SECURITY_GROUP_EGRESS_RULE: 1,
            R.SECURITY_GROUP_INGRESS_RULE: 2,
            R.ROLE: 1,
            R.ROLE_POLICY: 1,
            R.INSTANCE_PROFILE: 1,
            R.SSM_DOCUMENT: 1,
            R.EC2_INSTANCE: 1,
        }
    )


def test_temporary_program_refuses_credential_drift_before_resources(pulumi_mocks):
    journal = planned_journal()
    credentials = CapturedCredentials("999999999999", "us-east-1", "test-key", "test-secret", None)

    @pulumi.runtime.test
    def deploy():
        with raises(RuntimeError, match="credential identity differs"):
            access_program(journal, credentials)

    deploy()
    pulumi_mocks.assert_res_counts({})


def test_denied_claim_registers_no_resources(pulumi_mocks):
    journal = planned_journal()
    journal.require_claim.side_effect = RuntimeError("remote claim changed")
    credentials = CapturedCredentials("123456789012", "us-east-1", "test-key", "test-secret", None)

    @pulumi.runtime.test
    def deploy():
        with raises(RuntimeError, match="remote claim changed"):
            access_program(journal, credentials)

    deploy()
    pulumi_mocks.assert_res_counts({})


def test_remote_intent_reconstruction_preserves_creation_recipes(pulumi_mocks):
    intent = replace(
        planned_journal().intent,
        identity_document_content=json.dumps({"schemaVersion": "2.2", "description": "old-doc"}),
        ssm_policy_content=json.dumps({"Version": "2012-10-17", "Statement": []}),
        assume_role_policy_content=json.dumps({"Version": "2012-10-17", "Statement": []}),
        user_data_content="#!/bin/bash\nprintf 'recorded-bootstrap'\n",
    )
    storage = Storage()
    creator = AccessJournal(storage, "state-bucket", intent.account, intent)
    creator.initialize()
    # A separate caller has only persisted JSON, with current package defaults
    # deliberately different from the creation-time recipes.
    recovered = AccessIntent.from_dict(json.loads(storage.objects[intent.prefix + "intent.json"]))
    journal = AccessJournal(storage, "state-bucket", recovered.account, recovered)
    journal.claim()
    credentials = CapturedCredentials("123456789012", "us-east-1", "test-key", "test-secret", None)

    @pulumi.runtime.test
    def deploy():
        access_program(journal, credentials)

    deploy()
    for typ, suffix, limit, expected in (
        (R.ROLE, "-role", 64, {"assumeRolePolicy": intent.assume_role_policy_content}),
        (R.ROLE_POLICY, "-ssm", 64, {"policy": intent.ssm_policy_content}),
        (R.SSM_DOCUMENT, "-identity", 128, {"content": intent.identity_document_content}),
        (R.EC2_INSTANCE, "", 64, {"userData": intent.user_data_content}),
    ):
        pulumi_mocks.assert_res(
            resource_name(intent.name, suffix="-bastion" + suffix, limit=limit),
            typ,
            expected,
            partial=True,
            prefixed=False,
        )
    pulumi_mocks.assert_res_counts(
        {
            R.SECURITY_GROUP: 1,
            R.SECURITY_GROUP_EGRESS_RULE: 1,
            R.SECURITY_GROUP_INGRESS_RULE: 2,
            R.ROLE: 1,
            R.ROLE_POLICY: 1,
            R.INSTANCE_PROFILE: 1,
            R.SSM_DOCUMENT: 1,
            R.EC2_INSTANCE: 1,
        }
    )
