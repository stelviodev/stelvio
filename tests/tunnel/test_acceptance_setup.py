"""Evaluate generated proof configuration before running billable acceptance."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stelvio.app import StelvioApp
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc
from stelvio.component import ComponentRegistry
from tests.integration.cli_session import StandaloneCliSession


@pytest.mark.usefixtures("pulumi_mocks")
@pytest.mark.parametrize("no_vpc", [False, True])
def test_generated_proof_uses_current_component_configuration(tmp_path, monkeypatch, no_vpc):
    session = object.__new__(StandaloneCliSession)
    session.no_vpc = no_vpc
    session.invocation_path = tmp_path / "handler-imports.jsonl"
    session.project = tmp_path / "proof"
    session.app = "proof-app"
    session.environment_name = "tunnel-proof"
    session.profile = "default"
    session.region = "us-east-1"
    session._write_project()
    monkeypatch.chdir(session.project)
    monkeypatch.setattr(StelvioApp, "_StelvioApp__instance", None)
    monkeypatch.setattr("stelvio.export_output", Mock())
    monkeypatch.setattr(
        Vpc,
        "_create_resources",
        lambda self: SimpleNamespace(
            vpc=SimpleNamespace(id=f"{self.name}-id"),
            bastion=SimpleNamespace(id=f"{self.name}-bastion"),
        ),
    )
    monkeypatch.setattr(
        DocumentDb,
        "_create_resources",
        lambda self: SimpleNamespace(cluster=SimpleNamespace(id=f"{self.name}-cluster")),
    )
    monkeypatch.setattr(
        Function,
        "_create_resources",
        lambda self: SimpleNamespace(
            function=SimpleNamespace(name=self.name),
            function_url=SimpleNamespace(function_url=f"https://{self.name}.example.test"),
        ),
    )
    source = session.project / "stlv_app.py"
    namespace = {}
    exec(compile(source.read_text(), str(source), "exec"), namespace)  # noqa: S102 - generated test-owned application
    namespace["run"]()

    databases = [
        item for item in ComponentRegistry.all_instances() if isinstance(item, DocumentDb)
    ]
    if no_vpc:
        assert not databases
        assert not any(isinstance(item, Vpc) for item in ComponentRegistry.all_instances())
        assert sum(isinstance(item, Function) for item in ComponentRegistry.all_instances()) == 1
        return
    assert [item.name for item in databases] == ["docdb1", "docdb2"]
    assert [item.config.replicas for item in databases] == [1, 0]
    assert [1 + item.config.replicas for item in databases] == [2, 1]
    assert [item.config.vpc.name for item in databases] == ["net1", "net2"]
