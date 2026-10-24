"""Evaluate generated proof configuration before running billable acceptance."""

import re
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from stelvio.app import StelvioApp
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.document_db.document_db import _identifier_prefix
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc
from stelvio.component import ComponentRegistry
from stelvio.context import _ContextStore
from tests.integration import cli_session
from tests.integration.cli_session import StandaloneCliSession


@pytest.mark.usefixtures("pulumi_mocks")
@pytest.mark.parametrize("no_vpc", [False, True])
def test_generated_proof_uses_current_component_configuration(tmp_path, monkeypatch, no_vpc):
    monkeypatch.setenv("STLV_TEST_AWS_PROFILE", "default")
    monkeypatch.setenv("STLV_TEST_TUNNEL_AWS_ACCOUNT", "123456789012")
    monkeypatch.setenv("STLV_TEST_AWS_REGION", "us-east-1")
    # Stop at the AWS boundary after the real constructor selects the test names.
    monkeypatch.setattr(
        cli_session.boto3, "Session", Mock(side_effect=RuntimeError("AWS boundary"))
    )
    session = object.__new__(StandaloneCliSession)
    with pytest.raises(RuntimeError, match="AWS boundary"):
        session.__init__(tmp_path / "proof", no_vpc=no_vpc)
    assert re.fullmatch(r"stlv-t-[0-9a-f]{8}", session.app)
    assert session.environment_name == "proof"
    session.invocation_path = tmp_path / "handler-imports.jsonl"
    session._write_project()
    session.process = None
    session.cli = tmp_path / "stlv"
    session.log_path = session.project / "tunnel-start.log"
    popen = Mock()
    monkeypatch.setattr(cli_session.subprocess, "Popen", popen)
    try:
        session.start()
        assert popen.call_args.args[0] == [
            str(session.cli),
            "tunnel",
            "start",
            session.environment_name,
        ]
    finally:
        session._log_file.close()
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
    assert namespace["config"](session.environment_name).environments == [session.environment_name]
    assert (session.project / ".stelvio" / "userenv").read_text() == session.environment_name
    app_context = replace(_ContextStore.get(), name=session.app, env=session.environment_name)
    _ContextStore.clear()
    _ContextStore.set(app_context)
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

    for database in databases:
        suffixes = ["-", *(f"-{i}-" for i in range(1, database.config.replicas + 2))]
        for suffix in suffixes:
            prefix = _identifier_prefix(database.name, suffix=suffix)
            assert prefix == f"{session.app}-{session.environment_name}-{database.name}{suffix}"
            assert len(prefix) + 26 <= 63
