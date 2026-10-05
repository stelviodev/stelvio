"""The independent access program must never recreate application resources."""

from unittest.mock import Mock

import pulumi
from pytest import raises

from stelvio.tunnel.access_program import CapturedCredentials, access_program
from stelvio.tunnel.access_state import AccessIntent, AccessTarget
from tests.aws.pulumi_mocks import R


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
    intent = journal.intent
    group = pulumi_mocks.created(R.SECURITY_GROUP)[0]
    assert group.inputs == {
        "name": intent.name + "-sg",
        "vpcId": "vpc-application",
        "description": "Stelvio SSM access: no inbound SSH",
        "tags": intent.tags,
    }
    instance = pulumi_mocks.created(R.EC2_INSTANCE)[0]
    assert instance.inputs["ami"] == "ami-captured"
    assert instance.inputs["subnetId"] == "subnet-application"
    assert instance.inputs["vpcSecurityGroupIds"] == [group.name + "-test-id"]
    assert instance.inputs["availabilityZone"] == "us-east-1a"
    assert instance.inputs["tags"] == instance.inputs["volumeTags"] == intent.tags
    assert instance.inputs.get("keyName") is None
    for index, target in enumerate(intent.targets):
        # These root resources have no app/environment prefix: they belong to
        # the independently configured access backend, not a VPC component.
        rules = pulumi_mocks.created(R.SECURITY_GROUP_INGRESS_RULE, f"service-access-{index}")
        assert len(rules) == 1
        assert rules[0].inputs == {
            "securityGroupId": target.security_group_id,
            "referencedSecurityGroupId": group.name + "-test-id",
            "ipProtocol": "tcp",
            "fromPort": target.port,
            "toPort": target.port,
            "tags": intent.tags,
        }
    for typ, suffix in (
        (R.ROLE, "-role"),
        (R.INSTANCE_PROFILE, "-profile"),
        (R.SSM_DOCUMENT, "-identity"),
    ):
        resource = pulumi_mocks.created(typ)[0]
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
