from unittest.mock import Mock

from botocore.exceptions import ClientError
from pytest import fixture

from stelvio.tunnel.access_cleanup import AccessCleanup
from stelvio.tunnel.access_inventory import AccessInventory
from stelvio.tunnel.access_state import AccessIntent, AccessJournal, AccessTarget
from tests.tunnel.storage import VersionedStorage


@fixture
def access_intent():
    return AccessIntent(
        session="10e08906-0762-4410-9963-2c56a2e06400",
        unit="21f7a531",
        app="app",
        environment="dev",
        owner_uid=502,
        account="123456789012",
        region="us-east-1",
        vpc_id="vpc-target",
        subnet_id="subnet-public",
        availability_zone="us-east-1a",
        ami="ami-selected",
        targets=(AccessTarget("sg-database", 27018),),
    )


@fixture
def cleanup_world(access_intent):
    storage = VersionedStorage()
    journal = AccessJournal(storage, "state-bucket", access_intent.account, access_intent)
    journal.claim()
    journal.record("creation-finished.json", {"completed": True})
    tags = [{"Key": key, "Value": value} for key, value in access_intent.tags.items()]
    group = {
        "GroupId": "sg-owned",
        "GroupName": access_intent.name + "-sg",
        "VpcId": access_intent.vpc_id,
        "OwnerId": access_intent.account,
        "Tags": tags,
        "IpPermissions": [],
        "IpPermissionsEgress": [],
    }
    rule = {
        "SecurityGroupRuleId": "sgr-owned",
        "GroupId": "sg-database",
        "GroupOwnerId": access_intent.account,
        "IsEgress": False,
        "IpProtocol": "tcp",
        "FromPort": 27018,
        "ToPort": 27018,
        "ReferencedGroupInfo": {"GroupId": "sg-owned"},
        "Tags": tags,
    }
    resources = {"group": group, "rule": rule}
    session = Mock()
    clients = {name: Mock() for name in ("sts", "ec2", "iam", "ssm")}
    session.client.side_effect = lambda name, **_: clients[name]
    clients["sts"].get_caller_identity.return_value = {"Account": access_intent.account}
    ec2 = clients["ec2"]
    ec2.describe_volumes.return_value = {"Volumes": []}

    def paginator(operation):
        pages = Mock()
        if operation == "describe_instances":
            pages.paginate.return_value = [{"Reservations": []}]
        elif operation == "describe_security_group_rules":
            pages.paginate.side_effect = lambda **_: [
                {"SecurityGroupRules": [resources["rule"]] if resources["rule"] else []}
            ]
        else:
            raise AssertionError(operation)
        return pages

    ec2.get_paginator.side_effect = paginator
    ec2.describe_security_groups.side_effect = lambda **_: {
        "SecurityGroups": [resources["group"]] if resources["group"] else []
    }
    ec2.describe_security_group_rules.side_effect = lambda **_: {
        "SecurityGroupRules": [resources["rule"]] if resources["rule"] else []
    }

    def revoke(**request):
        assert journal.read("cleanup-observed.json")["rules"] == ["sgr-owned"]
        assert request == {"GroupId": "sg-database", "SecurityGroupRuleIds": ["sgr-owned"]}
        resources["rule"] = None

    def delete_group(**request):
        assert resources["rule"] is None
        assert journal.read("cleanup-observed.json")["group"] == "sg-owned"
        assert request == {"GroupId": "sg-owned"}
        resources["group"] = None

    ec2.revoke_security_group_ingress.side_effect = revoke
    ec2.delete_security_group.side_effect = delete_group
    for operation in ("get_role", "get_instance_profile"):
        getattr(clients["iam"], operation).side_effect = ClientError(
            {"Error": {"Code": "NoSuchEntity"}}, operation
        )
    clients["ssm"].describe_document.side_effect = ClientError(
        {"Error": {"Code": "InvalidDocument"}}, "DescribeDocument"
    )
    inventory = AccessInventory(journal, session)
    stopped = Mock()
    return AccessCleanup(inventory, stopped), journal, clients, resources, stopped
