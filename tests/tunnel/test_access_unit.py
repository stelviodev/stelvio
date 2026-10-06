"""Teardown refuses foreign checkpoint IDs before touching AWS effects."""

from types import SimpleNamespace
from unittest.mock import Mock

from pytest import mark, raises

from stelvio.tunnel.access_unit import AccessUnit


@mark.parametrize("pending", [False, True])
@mark.parametrize(
    ("kind", "identity"),
    [
        ("aws:ec2/securityGroup:SecurityGroup", "sg-foreign"),
        ("aws:ec2/instance:Instance", "i-foreign"),
        ("aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule", "sgr-foreign"),
        ("aws:iam/role:Role", "foreign-role"),
        ("aws:iam/instanceProfile:InstanceProfile", "foreign-profile"),
        ("aws:ssm/document:Document", "foreign-document"),
    ],
)
def test_foreign_checkpoint_identity_blocks_aws_and_pulumi_cleanup(
    cleanup_world, pending, kind, identity
):
    cleanup, journal, clients, _, stopped = cleanup_world
    provider = {
        "type": "pulumi:providers:aws",
        "urn": "urn:owned:provider",
        "id": "provider-id",
        "inputs": {"region": journal.intent.region, "allowedAccountIds": [journal.intent.account]},
    }
    resource = {
        "type": kind,
        "id": identity,
        "provider": "urn:owned:provider::provider-id",
        "inputs": {"tags": journal.intent.tags},
    }
    checkpoint = {
        "resources": [provider] + ([] if pending else [resource]),
        "pending_operations": [{"resource": resource}] if pending else [],
    }
    stack = Mock()
    stack.export_stack.return_value = SimpleNamespace(deployment=checkpoint)
    backend = Mock()
    backend.open.return_value = stack
    unit = AccessUnit(
        journal, backend, cleanup.inventory, Mock(), lambda _, operation: operation(), stopped
    )
    with raises(RuntimeError, match="Foreign physical identity"):
        unit.stop()
    clients["ec2"].revoke_security_group_ingress.assert_not_called()
    clients["ec2"].delete_security_group.assert_not_called()
    stack.refresh.assert_not_called()
    stack.destroy.assert_not_called()
    assert journal.read("claim.json") is not None


def test_owned_checkpoint_cleanup_uses_saved_ids_after_sdk_deletion(cleanup_world):
    cleanup, journal, _, resources, stopped = cleanup_world
    journal.record("creation-started.json", {"started": True})
    checkpoint = {
        "resources": [
            {
                "type": "pulumi:providers:aws",
                "urn": "urn:owned:provider",
                "id": "provider-id",
                "inputs": {
                    "region": journal.intent.region,
                    "allowedAccountIds": [journal.intent.account],
                },
            },
            {
                "type": "aws:ec2/securityGroup:SecurityGroup",
                "id": "sg-owned",
                "custom": True,
                "provider": "urn:owned:provider::provider-id",
                "inputs": {"tags": journal.intent.tags},
            },
        ]
    }
    stack = Mock()
    stack.export_stack.return_value = SimpleNamespace(deployment=checkpoint)

    def refresh(**request):
        assert request == {"clear_pending_creates": True}
        assert journal.read("aws-cleaned.json") is not None
        assert resources == {"group": None, "rule": None}
        stack.export_stack.return_value = SimpleNamespace(deployment={"resources": []})

    stack.refresh.side_effect = refresh
    backend = Mock()
    backend.open.return_value = stack
    backend.remove_key.side_effect = lambda: journal.record("key-removed.json", {"removed": True})
    operations = []

    def run(label, operation):
        if label.startswith("access-"):
            # Every invocation receives a new workspace environment snapshot.
            assert backend.refresh_credentials.call_count == len(operations) + 1
            operations.append(label)
        return operation()

    unit = AccessUnit(journal, backend, cleanup.inventory, Mock(), run, stopped)
    unit.stop()
    assert operations == [
        "access-cancel",
        "access-checkpoint",
        "access-checkpoint",
        "access-refresh",
        "access-checkpoint",
        "access-destroy",
        "access-checkpoint",
    ]
    stack.destroy.assert_called_once_with()
    assert journal.read("claim.json") is None
    assert journal.s3.versions == []
    unit.stop()
    stack.destroy.assert_called_once_with()


def test_disposal_resumes_after_backend_versions_were_already_removed(cleanup_world):
    cleanup, journal, _, _, stopped = cleanup_world
    journal.record("backend-cleaned.json", {"cleaned": True})
    backend = Mock()
    backend.open.side_effect = AssertionError("deleted backend must not be reopened")
    backend.remove_key.side_effect = lambda: journal.record("key-removed.json", {"removed": True})
    unit = AccessUnit(
        journal, backend, cleanup.inventory, Mock(), lambda _, operation: operation(), stopped
    )
    unit.stop()
    backend.open.assert_not_called()
    backend.remove_key.assert_called_once_with()
    assert journal.s3.versions == []
