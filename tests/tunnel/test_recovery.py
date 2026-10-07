"""Stale AWS cleanup preserves live/foreign ownership and continues other units."""

import os
from dataclasses import asdict, replace
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import Mock

from click.testing import CliRunner
from pytest import fixture, mark

from stelvio.tunnel.access_state import AccessJournal
from stelvio.tunnel.processes import identity
from tests.tunnel.storage import VersionedStorage


@fixture
def recovery_world(access_intent, monkeypatch, tmp_path, cli):
    module = import_module("stelvio.cli.tunnel")
    storage = VersionedStorage()
    session = Mock()
    session.region_name = access_intent.region
    sts = Mock()
    sts.get_caller_identity.return_value = {"Account": access_intent.account}
    session.client.side_effect = lambda service, **kw: {"sts": sts, "s3": storage}[service]
    monkeypatch.setattr(module.boto3, "Session", lambda **kw: session)
    monkeypatch.setattr(module, "get_stelvio_config_dir", lambda: tmp_path)
    intent = replace(access_intent, owner_uid=os.geteuid())
    args = [
        "recover",
        "--bucket",
        "state-bucket",
        "--home-account",
        intent.account,
        "--home-region",
        intent.region,
        "--account",
        intent.account,
        "--region",
        intent.region,
        "--app",
        intent.app,
        "--env",
        intent.environment,
        "--session",
        intent.session,
    ]
    return SimpleNamespace(storage=storage, intent=intent, args=args, command=module.tunnel)


def disposed(world, intent):
    journal = AccessJournal(world.storage, "state-bucket", intent.account, intent)
    journal.record("metadata-cleanup.json", {"intent": intent.to_dict(), "cleaned": True})
    return journal


def test_recovery_continues_after_live_owner_refusal_and_preserves_foreign_session(recovery_world):
    world = recovery_world
    active = AccessJournal(world.storage, "state-bucket", world.intent.account, world.intent)
    active.record("intent.json", world.intent.to_dict())
    active.record("claim.json", {"creator": asdict(identity(os.getpid())), "token": "original"})
    clean = replace(world.intent, unit="ffffffff")
    disposed(world, clean)
    foreign = replace(world.intent, session="a1111111-1111-4111-8111-111111111111")
    foreign_journal = disposed(world, foreign)
    before = foreign_journal.read("metadata-cleanup.json")

    result = CliRunner().invoke(world.command, world.args)
    assert result.exit_code == 1
    assert "Previous network creator is still live" in result.output
    assert f"Recovered {clean.unit} VPC {clean.vpc_id}" in result.output
    assert active.read("claim.json")["token"] == "original"  # noqa: S105 - ownership nonce fixture
    assert foreign_journal.read("metadata-cleanup.json") == before
    assert not any(v["Key"].startswith(clean.prefix) for v in world.storage.versions)


def test_recovery_resumes_metadata_only_disposal_without_original_intent(recovery_world):
    world = recovery_world
    disposed(world, world.intent)
    result = CliRunner().invoke(world.command, world.args)
    assert result.exit_code == 0
    assert result.output == f"Recovered {world.intent.unit} VPC {world.intent.vpc_id}\n"
    assert world.storage.versions == []
    assert CliRunner().invoke(world.command, world.args).exit_code == 0


def test_recovery_refuses_foreign_uid_and_other_region_as_incomplete(recovery_world):
    world = recovery_world
    foreign = replace(world.intent, owner_uid=world.intent.owner_uid + 1)
    disposed(world, foreign)
    other = replace(world.intent, unit="ffffffff", region="eu-west-1")
    disposed(world, other)
    original = list(world.storage.versions)
    result = CliRunner().invoke(world.command, world.args)
    assert result.exit_code == 1
    assert "selected namespace or owner UID" in result.output
    assert "--region eu-west-1" in result.output
    assert world.storage.versions == original


@mark.parametrize("hidden", [False, True])
def test_recovery_missing_claim_with_creation_evidence_retains_records(recovery_world, hidden):
    world = recovery_world
    journal = AccessJournal(world.storage, "state-bucket", world.intent.account, world.intent)
    journal.initialize()
    journal.record("creation-started.json", {"started": True})
    if hidden:
        key = world.intent.prefix + "creation-started.json"
        world.storage.delete_object(
            ExpectedBucketOwner=world.intent.account,
            Key=key,
            IfMatch=world.storage.get_object(ExpectedBucketOwner=world.intent.account, Key=key)[
                "ETag"
            ],
        )
    original = list(world.storage.versions)
    result = CliRunner().invoke(world.command, world.args)
    assert result.exit_code == 1
    assert "Previous claim is missing with actor or creation evidence" in result.output
    assert world.storage.versions == original
