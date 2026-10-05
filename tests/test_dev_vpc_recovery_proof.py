"""Lost-response recovery must observe effects before certifying cleanup."""

from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

from pytest import raises

proof = import_module("spikes.dev-vpc-v1.aws_recovery")


def fixture_state():
    state = Mock()
    state.get.return_value = None
    state.filters.return_value = []
    state.ec2.describe_security_group_rules.return_value = {"SecurityGroupRules": []}
    return state


def test_unobserved_creation_retains_intent(monkeypatch):
    state = fixture_state()
    state.group.return_value = None
    clock = iter([0, 121])
    monkeypatch.setattr(proof, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with raises(RuntimeError, match="never observed"):
        proof.observe_case(
            state, "group", "vpc-1", {"name": "planned", "description": "d", "target": "sg-app"}
        )
    state.put.assert_not_called()
    state.ec2.delete_security_group.assert_not_called()


def test_delayed_creation_records_identity_before_deletion(monkeypatch):
    state = fixture_state()
    group = {"GroupId": "sg-1", "IpPermissions": []}
    state.group.side_effect = [None, group]
    monkeypatch.setattr(proof, "time", SimpleNamespace(monotonic=lambda: 0, sleep=lambda _: None))
    plan = {"name": "planned", "description": "d", "target": "sg-app"}
    observed = proof.observe_case(state, "group", "vpc-1", plan)
    assert observed == {"plan": plan, "group": "sg-1", "rules": []}
    state.put.assert_called_once_with("observed-group.json", observed)
    state.ec2.delete_security_group.assert_not_called()


def test_empty_filter_cannot_certify_known_resource_deletion(monkeypatch):
    state = fixture_state()
    state.group.return_value = None
    state.ec2.describe_security_groups.return_value = {"SecurityGroups": [{"GroupId": "sg-1"}]}
    clock = iter([0, 121])
    monkeypatch.setattr(proof, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    with raises(RuntimeError, match="not confirmed"):
        proof.confirm_case(
            state,
            "group",
            "vpc-1",
            {"name": "p", "description": "d", "target": "sg-app"},
            {"group": "sg-1", "rules": []},
        )


def test_recorded_cleanup_refuses_replacement_group():
    state = fixture_state()
    plan = {"name": "p", "description": "d", "target": "sg-app"}
    state.group.return_value = {"GroupId": "sg-foreign", "IpPermissions": []}
    with raises(RuntimeError, match="differs from recorded"):
        proof.delete_case(state, "group", "vpc-1", plan, {"plan": plan, "group": "sg-1"})
    state.ec2.delete_security_group.assert_not_called()


def test_partial_rule_group_can_be_cleaned_without_claiming_rule_proof(monkeypatch):
    state = fixture_state()
    state.group.return_value = {"GroupId": "sg-1", "IpPermissions": []}
    clock = iter([0, 121])
    monkeypatch.setattr(proof, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    observed = proof.observe_case(
        state,
        "rule",
        "vpc-1",
        {"name": "p", "description": "d", "target": "sg-app"},
        allow_partial=True,
    )
    assert observed["group"] == "sg-1"
    assert observed["rules"] == []


def test_resumed_cleanup_refuses_modified_rule():
    state = fixture_state()
    plan = {"name": "p", "description": "d", "target": "sg-app"}
    state.group.return_value = {"GroupId": "sg-1", "IpPermissions": []}
    state.ec2.describe_security_group_rules.return_value = {
        "SecurityGroupRules": [
            {
                "SecurityGroupRuleId": "sgr-1",
                "IsEgress": False,
                "IpProtocol": "tcp",
                "FromPort": 22,
                "ToPort": 22,
                "ReferencedGroupInfo": {"GroupId": "sg-1"},
            }
        ]
    }
    with raises(RuntimeError, match="differs from planned"):
        proof.delete_case(
            state,
            "rule",
            "vpc-1",
            plan,
            {"plan": plan, "group": "sg-1", "rules": ["sgr-1"]},
        )
    state.ec2.revoke_security_group_ingress.assert_not_called()


def test_recovery_journals_before_delete_and_confirms_disappearance():
    state = fixture_state()
    state.args = SimpleNamespace(owner="proof-owner")
    plan = {
        "vpc": "vpc-1",
        "target": "sg-app",
        "name": "stlv-proof-access-proof-owner-group",
        "description": "P0 lost-response recovery group",
        "case": "group",
    }
    journal = {"pending-group.json": plan}
    events = []
    resource = {"GroupId": "sg-1", "IpPermissions": []}
    alive = True

    def get(key, **kwargs):
        return journal.get(key)

    def put(key, value):
        journal[key] = value
        events.append("journal")

    def group(*args):
        return resource if alive else None

    def delete(**kwargs):
        nonlocal alive
        assert kwargs == {"GroupId": "sg-1"}
        assert journal["observed-group.json"]["group"] == "sg-1"
        events.append("delete")
        alive = False

    def describe(**kwargs):
        assert kwargs == {"GroupIds": ["sg-1"]}
        events.append("confirm")
        return {"SecurityGroups": [resource] if alive else []}

    state.get.side_effect = get
    state.put.side_effect = put
    state.group.side_effect = group
    state.ec2.delete_security_group.side_effect = delete
    state.ec2.describe_security_groups.side_effect = describe
    result = proof.recover_case(state, "group", "vpc-1", {"GroupId": "sg-app"})
    assert result == {"case": "group", "group": "sg-1", "rules": []}
    assert not alive
    assert events == ["journal", "delete", "confirm"]
