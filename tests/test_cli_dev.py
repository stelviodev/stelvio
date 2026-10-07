"""The dev command owns networking across deployment, bridge exit and interruption."""

import asyncio
import time
from threading import Event
from types import SimpleNamespace

from click.testing import CliRunner
from pytest import mark, raises

from stelvio.bridge.local.listener import run_bridge_server
from stelvio.command_run import CommandRun
from tests.cli_test_helpers import FakeCommandRun


@mark.parametrize("mode", [[], ["--network-mode", "auto"]])
def test_dev_default_and_explicit_auto(cli, mode):
    result = CliRunner().invoke(cli.dev, ["dev", "--yes", *mode])
    assert result.exit_code == 0
    cli.run_dev.assert_called_once_with("dev", show_unchanged=False, network_mode="auto")


@mark.parametrize("mode", ["external", "managed", "invalid"])
def test_dev_rejects_unsupported_modes_before_setup(cli, mode):
    result = CliRunner().invoke(cli.dev, ["dev", "--network-mode", mode])
    assert result.exit_code == 2
    cli.ensure_pulumi.assert_not_called()
    cli.run_dev.assert_not_called()


@mark.parametrize("ending", [None, KeyboardInterrupt(), RuntimeError("bridge disconnected")])
def test_dev_captures_before_deployment_cleanup_and_closes_after_bridge(
    cli_commands, monkeypatch, ending
):
    events = []

    def start():
        events.append("network-start")

    def admit(endpoint):
        return None

    class Run(FakeCommandRun):
        def capture_network_session(self, report):
            events.append("capture")
            return SimpleNamespace(start=start, admit=admit, close=lambda: events.append("close"))

        def __exit__(self, *exc):
            events.append("deployment-cleanup")
            return False

    cli_commands.CommandRun.return_value = Run({})
    monkeypatch.setattr(cli_commands.ProviderStore, "region", lambda: "us-east-1")

    def bridge(**options):
        events.append("bridge")
        assert options == {
            "region": "us-east-1",
            "profile": "default",
            "app_name": "test",
            "env": "dev",
            "admission": admit,
        }
        if ending:
            raise ending

    monkeypatch.setattr(cli_commands, "run_bridge_server", bridge)
    if ending:
        with raises(type(ending), match=str(ending)):
            cli_commands.run_dev("dev")
    else:
        cli_commands.run_dev("dev")
    assert events == ["capture", "deployment-cleanup", "network-start", "bridge", "close"]


def test_dev_interrupt_with_running_handler_reaches_network_cleanup(cli_commands, monkeypatch):
    running = Event()
    release = Event()
    done = Event()
    closed = []

    def handler():
        running.set()
        try:
            release.wait(timeout=15)
        finally:
            done.set()

    async def bridge(*args):
        loop = asyncio.get_running_loop()

        async def interrupt():
            while not running.is_set():
                await asyncio.sleep(0.01)
            # Simulate the interruption of the synchronous bridge driver while
            # it is awaiting its executor. The handler remains alive until release.
            loop.call_soon(lambda: (_ for _ in ()).throw(KeyboardInterrupt()))

        interrupter = loop.create_task(interrupt())
        try:
            await loop.run_in_executor(None, handler)
        finally:
            interrupter.cancel()

    def close():
        closed.append(running.is_set() and not done.is_set())

    run = FakeCommandRun({})
    run.capture_network_session = lambda report: SimpleNamespace(
        start=lambda: None, admit=lambda endpoint: None, close=close
    )
    cli_commands.CommandRun.return_value = run
    monkeypatch.setattr(cli_commands.ProviderStore, "region", lambda: "us-east-1")
    monkeypatch.setattr("stelvio.bridge.local.listener.main", bridge)
    monkeypatch.setattr(cli_commands, "run_bridge_server", run_bridge_server)
    started = time.monotonic()
    try:
        with raises(KeyboardInterrupt, match=""):
            cli_commands.run_dev("dev")
        assert time.monotonic() - started < 5
        assert closed == [True]
    finally:
        release.set()
        assert done.wait(timeout=3)


def test_command_run_non_vpc_capture_does_not_query_home(monkeypatch, tmp_path):
    run = FakeCommandRun({})
    run.env = "dev"
    run._home = None
    monkeypatch.setattr("stelvio.command_run.get_stelvio_config_dir", lambda: tmp_path)
    network = CommandRun.capture_network_session(run, lambda message: None)
    network.start()
    assert network.admit("non-vpc") is None
    network.close()


def test_dev_global_network_startup_failure_never_starts_bridge(cli_commands, monkeypatch):
    events = []

    def start():
        events.append("network-start")
        raise RuntimeError("global helper unavailable")

    class Run(FakeCommandRun):
        def capture_network_session(self, report):
            events.append("capture")
            return SimpleNamespace(
                start=start, admit=lambda endpoint: None, close=lambda: events.append("close")
            )

        def __exit__(self, *exc):
            events.append("deployment-cleanup")
            return False

    cli_commands.CommandRun.return_value = Run({})
    monkeypatch.setattr(cli_commands.ProviderStore, "region", lambda: "us-east-1")
    monkeypatch.setattr(cli_commands, "run_bridge_server", lambda **kw: events.append("bridge"))
    with raises(RuntimeError, match="global helper unavailable"):
        cli_commands.run_dev("dev")
    assert events == ["capture", "deployment-cleanup", "network-start", "close"]
