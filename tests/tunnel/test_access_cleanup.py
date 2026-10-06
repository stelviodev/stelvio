"""AWS cleanup boundary preserves workloads and durable records on failure."""

from datetime import UTC, datetime
from unittest.mock import Mock

from botocore.exceptions import ClientError
from pytest import mark, raises


def test_cleanup_records_lost_create_ids_before_deletion_and_preserves_application(cleanup_world):
    cleanup, journal, clients, resources, stopped = cleanup_world
    expected = cleanup.remove()
    assert expected["group"] == "sg-owned"
    assert expected["rules"] == ["sgr-owned"]
    assert resources == {"group": None, "rule": None}
    assert journal.read("aws-cleaned.json") is not None
    assert journal.read("claim.json") is not None
    assert stopped.call_count >= 1
    clients["ec2"].delete_vpc.assert_not_called()
    clients["ec2"].delete_subnet.assert_not_called()
    clients["ec2"].revoke_security_group_egress.assert_not_called()
    cleanup.remove()
    clients["ec2"].delete_security_group.assert_called_once_with(GroupId="sg-owned")


def test_live_engine_blocks_cleanup_before_discovery_or_mutation(cleanup_world):
    cleanup, journal, clients, _, stopped = cleanup_world
    stopped.side_effect = RuntimeError("creating engine is still live")
    with raises(RuntimeError, match="engine is still live"):
        cleanup.remove()
    clients["ec2"].describe_security_groups.assert_not_called()
    clients["ec2"].delete_security_group.assert_not_called()
    assert journal.read("cleanup-observed.json") is None


def test_modified_target_rule_refuses_cleanup_before_any_mutation(cleanup_world):
    cleanup, journal, clients, resources, _ = cleanup_world
    resources["rule"]["ToPort"] = 22
    with raises(RuntimeError, match="differs from planned ingress"):
        cleanup.remove()
    clients["ec2"].revoke_security_group_ingress.assert_not_called()
    clients["ec2"].delete_security_group.assert_not_called()
    assert journal.read("cleanup-observed.json") is None


def test_retry_after_partial_rule_deletion_retains_and_uses_exact_ids(cleanup_world):
    cleanup, journal, clients, resources, _ = cleanup_world
    original = clients["ec2"].revoke_security_group_ingress.side_effect

    def committed_without_response(**request):
        original(**request)
        raise TimeoutError("lost successful revoke response")

    clients["ec2"].revoke_security_group_ingress.side_effect = committed_without_response
    with raises(TimeoutError, match="lost successful"):
        cleanup.remove()
    assert resources["rule"] is None
    assert journal.read("cleanup-observed.json")["rules"] == ["sgr-owned"]
    assert journal.read("aws-cleaned.json") is None
    cleanup.remove()
    assert journal.read("aws-cleaned.json") is not None


def test_known_group_cannot_disappear_only_from_tag_discovery(cleanup_world):
    cleanup, journal, clients, _, _ = cleanup_world
    journal.record(
        "cleanup-observed.json",
        {
            "group": "sg-owned",
            "instance": None,
            "rules": [],
            "role": None,
            "volumes": [],
            "profile": None,
            "document": None,
        },
    )
    clients["ec2"].get_paginator.return_value.paginate.return_value = []
    clients["ec2"].describe_security_groups.side_effect = lambda **request: {
        "SecurityGroups": [] if "Filters" in request else [{"GroupId": "sg-owned"}]
    }
    clients["ec2"].get_paginator.side_effect = lambda operation: Mock(
        **{
            "paginate.return_value": [{"Reservations": []}]
            if operation == "describe_instances"
            else [{"SecurityGroupRules": []}]
        }
    )
    with raises(RuntimeError, match="lost ownership"):
        cleanup.remove()
    clients["ec2"].delete_security_group.assert_not_called()
    assert journal.read("aws-cleaned.json") is None


def test_foreign_instance_profile_association_preserves_its_credentials(cleanup_world):
    cleanup, journal, clients, _, _ = cleanup_world
    intent = journal.intent
    profile = {
        "InstanceProfileId": "AIP-owned",
        "InstanceProfileName": intent.name + "-profile",
        "Arn": f"arn:aws:iam::{intent.account}:instance-profile/{intent.name}-profile",
        "Roles": [],
    }
    iam = clients["iam"]
    iam.get_instance_profile.side_effect = None
    iam.get_instance_profile.return_value = {"InstanceProfile": profile}
    iam.list_instance_profile_tags.return_value = {
        "Tags": [{"Key": key, "Value": value} for key, value in intent.tags.items()]
    }
    original = clients["ec2"].get_paginator.side_effect

    def paginator(operation):
        if operation == "describe_iam_instance_profile_associations":
            return Mock(
                **{
                    "paginate.return_value": [
                        {
                            "IamInstanceProfileAssociations": [
                                {
                                    "InstanceId": "i-foreign",
                                    "IamInstanceProfile": {"Arn": profile["Arn"]},
                                    "State": "associated",
                                }
                            ]
                        }
                    ]
                }
            )
        return original(operation)

    clients["ec2"].get_paginator.side_effect = paginator
    with raises(RuntimeError, match="Foreign profile association"):
        cleanup.remove()
    iam.remove_role_from_instance_profile.assert_not_called()
    iam.delete_instance_profile.assert_not_called()
    iam.delete_role.assert_not_called()
    assert journal.read("aws-cleaned.json") is None
    assert journal.read("claim.json") is not None


def test_document_recovery_uses_recorded_content_after_package_changes(cleanup_world, monkeypatch):
    cleanup, journal, clients, _, _ = cleanup_world
    ssm = clients["ssm"]
    document = {
        "Name": journal.intent.name + "-identity",
        "DocumentType": "Command",
        "CreatedDate": datetime(2026, 10, 5, tzinfo=UTC),
        "Hash": "creation-profile-hash",
        "DefaultVersion": "1",
    }
    alive = True

    def describe(**_):
        if not alive:
            raise ClientError({"Error": {"Code": "InvalidDocument"}}, "DescribeDocument")
        return {"Document": document}

    def delete(**request):
        nonlocal alive
        assert request == {"Name": document["Name"]}
        alive = False

    ssm.describe_document.side_effect = describe
    ssm.get_document.return_value = {"Content": journal.intent.identity_document_content}
    ssm.list_tags_for_resource.return_value = {
        "TagList": [{"Key": key, "Value": value} for key, value in journal.intent.tags.items()]
    }
    ssm.delete_document.side_effect = delete
    monkeypatch.setattr("stelvio.tunnel.bastion.identity_document", lambda: "new package content")
    cleanup.remove()
    assert not alive
    assert journal.read("aws-cleaned.json") is not None


@mark.parametrize("foreign_attachment", [False, True])
def test_root_volume_mapping_survives_missing_tags_and_refuses_foreign_attachment(
    cleanup_world, foreign_attachment
):
    cleanup, journal, clients, _, _ = cleanup_world
    ec2 = clients["ec2"]
    intent = journal.intent
    instance = {
        "InstanceId": "i-owned",
        "VpcId": intent.vpc_id,
        "SubnetId": intent.subnet_id,
        "ImageId": intent.ami,
        "Placement": {"AvailabilityZone": intent.availability_zone},
        "State": {"Name": "running"},
        "MetadataOptions": {"HttpTokens": "required"},
        "IamInstanceProfile": {
            "Arn": f"arn:aws:iam::{intent.account}:instance-profile/{intent.name}-profile",
        },
        "SecurityGroups": [{"GroupId": "sg-owned"}],
        "Tags": [{"Key": key, "Value": value} for key, value in intent.tags.items()],
        "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-owned"}}],
    }
    volume = {
        "VolumeId": "vol-owned",
        "Encrypted": True,
        "Attachments": [{"InstanceId": "i-foreign" if foreign_attachment else "i-owned"}],
    }
    alive = True
    original = ec2.get_paginator.side_effect

    def paginator(operation):
        if operation == "describe_instances":
            return Mock(
                **{
                    "paginate.side_effect": lambda **_: [
                        {"Reservations": [{"Instances": [instance]}]},
                    ]
                }
            )
        return original(operation)

    def describe_volumes(**request):
        if "Filters" in request:
            return {"Volumes": []}
        assert request == {"VolumeIds": ["vol-owned"]}
        if not alive:
            raise ClientError({"Error": {"Code": "InvalidVolume.NotFound"}}, "DescribeVolumes")
        return {"Volumes": [volume]}

    def terminate(**request):
        assert request == {"InstanceIds": ["i-owned"]}
        assert journal.read("cleanup-observed.json")["volumes"] == ["vol-owned"]
        instance["State"]["Name"] = "terminated"
        volume["Attachments"] = []

    def delete_volume(**request):
        nonlocal alive
        assert request == {"VolumeId": "vol-owned"}
        assert instance["State"]["Name"] == "terminated"
        alive = False

    ec2.get_paginator.side_effect = paginator
    ec2.describe_instances.side_effect = lambda **_: {"Reservations": [{"Instances": [instance]}]}
    ec2.describe_volumes.side_effect = describe_volumes
    ec2.terminate_instances.side_effect = terminate
    ec2.delete_volume.side_effect = delete_volume
    if foreign_attachment:
        with raises(RuntimeError, match="foreign attachment"):
            cleanup.remove()
        ec2.terminate_instances.assert_not_called()
        ec2.delete_volume.assert_not_called()
        assert journal.read("cleanup-observed.json") is None
    else:
        cleanup.remove()
        ec2.delete_volume.assert_called_once_with(VolumeId="vol-owned")
        assert journal.read("aws-cleaned.json") is not None
