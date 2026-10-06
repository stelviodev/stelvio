"""The command boundary isolates credentials and tears down on output failure."""

import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from pytest import fixture, raises

from stelvio.tunnel.access_program import CapturedCredentials
from stelvio.tunnel.engine import TrackedPulumiCommand


@fixture
def native_command(monkeypatch, tmp_path):
    monkeypatch.setattr(subprocess, "check_output", lambda *_, **__: b"v3.263.0\n")
    stub = Mock()
    stub.GetPluginInfo.return_value = SimpleNamespace(version="7.47.0")
    monkeypatch.setattr("stelvio.tunnel.engine.ResourceProviderStub", lambda _: stub)
    monkeypatch.setattr("stelvio.tunnel.engine._ENGINE_SECONDS", 2)
    captures = []
    children = []

    class Registry:
        cli = tmp_path / "bin/pulumi"
        journal = SimpleNamespace(
            intent=SimpleNamespace(account="123456789012", region="us-east-1")
        )

        def require_stopped(self):
            assert all(child.poll() is not None for child in children)

        def spawn(self, kind, args, environment, cwd=None):
            captures.append((kind, dict(environment)))
            script = (
                "import time; print('12345', flush=True); time.sleep(60)"
                if kind == "aws-provider"
                else "import sys; print('first', flush=True); "
                "[print('x'*16384, flush=True) for _ in range(64)]"
            )
            child = subprocess.Popen(  # noqa: S603 - controlled stream-only fixture script
                [sys.executable, "-u", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                text=True,
                cwd=cwd,
            )
            children.append(child)
            return child

        def stop(self, child):
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    registry = Registry()
    generation = iter(["target-first", "target-second", "target-drift"])
    account = "123456789012"

    def capture():
        return CapturedCredentials(account, "us-east-1", next(generation), "fake-secret", None)

    def set_account():
        nonlocal account
        account = "999999999999"

    command = TrackedPulumiCommand(registry, provider_credentials=capture)
    try:
        yield command, captures, children, set_account
    finally:
        for child in children:
            registry.stop(child)
            for stream in (child.stdout, child.stderr):
                if not stream.closed:
                    stream.close()


def test_output_callback_failure_drains_streams_and_stops_all_actors(native_command, tmp_path):
    command, captures, children, _ = native_command

    def fail(_):
        raise ValueError("output consumer failed")

    with raises(ValueError, match="output consumer failed"):
        command.run(
            ["preview"],
            str(tmp_path),
            {"AWS_ACCESS_KEY_ID": "fake-home", "AWS_SECRET_ACCESS_KEY": "fake-home-secret"},
            on_output=fail,
        )
    assert [kind for kind, _ in captures] == ["aws-provider", "engine"]
    assert all(child.poll() is not None for child in children)


def test_each_command_renews_target_credentials_and_isolates_home_context(
    native_command, tmp_path, monkeypatch
):
    command, captures, children, drift = native_command
    monkeypatch.setenv("PULUMI_BACKEND_URL", "s3://foreign/application")
    monkeypatch.setenv("PULUMI_CONFIG_PASSPHRASE_FILE", "/foreign/phrase")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", "/foreign/token")
    monkeypatch.setenv("AWS_PROFILE", "handler-selected-profile")
    environment = {
        "AWS_ACCESS_KEY_ID": "fake-home-key",
        "AWS_SECRET_ACCESS_KEY": "fake-home-secret",
        "PULUMI_BACKEND_URL": "s3://owned/tunnel/backend",
        "PULUMI_CONFIG_PASSPHRASE": "fake-owned-phrase",
    }
    for target in ("target-first", "target-second"):
        assert command.run(["preview"], str(tmp_path), environment).code == 0
        provider, engine = captures[-2:]
        assert provider[0] == "aws-provider"
        assert engine[0] == "engine"
        assert provider[1]["AWS_ACCESS_KEY_ID"] == target
        assert engine[1]["AWS_ACCESS_KEY_ID"] == "fake-home-key"
        assert engine[1]["PULUMI_BACKEND_URL"] == "s3://owned/tunnel/backend"
        assert engine[1]["PULUMI_CONFIG_PASSPHRASE"] == environment["PULUMI_CONFIG_PASSPHRASE"]
        assert engine[1]["PULUMI_DEBUG_PROVIDERS"] == "aws:12345"
        for _, captured in (provider, engine):
            assert captured["AWS_CONFIG_FILE"] == os.devnull
            assert captured["AWS_SHARED_CREDENTIALS_FILE"] == os.devnull
            assert captured["AWS_PROFILE"] == ""
            assert "AWS_WEB_IDENTITY_TOKEN_FILE" not in captured
            assert "PULUMI_CONFIG_PASSPHRASE_FILE" not in captured
        assert "PULUMI_BACKEND_URL" not in provider[1]
        assert "PULUMI_CONFIG_PASSPHRASE" not in provider[1]
    drift()
    with raises(RuntimeError, match="credential context changed"):
        command.run(["preview"], str(tmp_path), environment)
    assert len(captures) == 4
    assert all(child.poll() is not None for child in children)
