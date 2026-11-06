"""Shutdown behavior through the CLI and synchronous bridge entry point."""

import asyncio
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from threading import Thread
from unittest.mock import AsyncMock, MagicMock

import pytest

from stelvio.bridge.local import listener


@pytest.mark.parametrize("interrupt_phase", ["parent", "worker"])
def test_cli_interrupt_exits_with_blocked_handler(interrupt_phase, tmp_path):
    if interrupt_phase == "worker" and not hasattr(signal, "pthread_kill"):
        pytest.skip("Thread-directed signals require pthread_kill")
    with subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-u",
            "-m",
            "tests.bridge.shutdown_child",
            interrupt_phase,
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.PIPE,
    ) as child:
        output = b""
        try:
            deadline = time.monotonic() + 15
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                ready = b"LOOP_WAITING\n" if interrupt_phase == "worker" else b"HANDLER_STARTED\n"
                while ready not in output:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, "Child did not start its handler"
                    assert selector.select(remaining), "Child did not start its handler"
                    chunk = os.read(child.stdout.fileno(), 4096)
                    assert chunk, "Child closed stdout before readiness"
                    output += chunk

            shutdown_deadline = time.monotonic() + (1 if interrupt_phase == "parent" else 5)
            if interrupt_phase == "parent":
                child.send_signal(signal.SIGINT)
            if interrupt_phase == "worker":
                child.stdin.write(b"\n")
                child.stdin.flush()
            stdout, stderr = child.communicate(
                timeout=max(0, shutdown_deadline - time.monotonic())
            )
            diagnostics = f"stdout:\n{(output + stdout).decode()}\nstderr:\n{stderr.decode()}"
            assert child.returncode == 1, diagnostics
            assert stderr == b"\nAborted!\n", diagnostics
        except (AssertionError, subprocess.TimeoutExpired) as error:
            if child.poll() is None:
                child.kill()
            stdout, stderr = child.communicate(timeout=5)
            reason = (
                "Ctrl+C did not stop stlv dev while a handler was blocked"
                if isinstance(error, subprocess.TimeoutExpired)
                else str(error)
            )
            pytest.fail(
                f"{reason}\n"
                f"stdout:\n{(output + stdout).decode(errors='replace')}\n"
                f"stderr:\n{stderr.decode(errors='replace')}",
                pytrace=False,
            )
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


def test_second_ctrl_c_interrupts_immediately(monkeypatch):
    second_interrupted = False

    async def main(**_kwargs):
        nonlocal second_interrupted
        signal.raise_signal(signal.SIGINT)
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            with pytest.raises(KeyboardInterrupt):
                signal.raise_signal(signal.SIGINT)
            second_interrupted = True
            raise

    monkeypatch.setattr(listener, "main", main)
    with pytest.raises(KeyboardInterrupt):
        listener.run_bridge_server("us-east-1", "default", "app", "dev")
    assert second_interrupted


@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "interrupt"])
def test_bridge_restores_signal_state_and_closes_loop(monkeypatch, outcome):
    previous_handler = signal.getsignal(signal.SIGINT)
    loops = []

    async def main(**_kwargs):
        loops.append(asyncio.get_running_loop())
        if outcome == "interrupt":
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0)
        elif outcome == "cancel":
            raise asyncio.CancelledError
        elif outcome == "error":
            raise ValueError("bridge failed")

    monkeypatch.setattr(listener, "main", main)
    expected = {
        "success": None,
        "error": ValueError,
        "cancel": asyncio.CancelledError,
        "interrupt": KeyboardInterrupt,
    }[outcome]
    reader, writer = socket.socketpair()
    with reader, writer:
        writer.setblocking(False)
        previous_fd = signal.set_wakeup_fd(writer.fileno())
        try:
            if expected is None:
                listener.run_bridge_server("us-east-1", "default", "app", "dev")
            else:
                with pytest.raises(expected):
                    listener.run_bridge_server("us-east-1", "default", "app", "dev")
            restored_fd = signal.set_wakeup_fd(writer.fileno())
            assert restored_fd == writer.fileno()
            assert signal.getsignal(signal.SIGINT) is previous_handler
            assert len(loops) == 1
            assert loops[0].is_closed()
        finally:
            signal.set_wakeup_fd(previous_fd)
            signal.signal(signal.SIGINT, previous_handler)


def test_bridge_runs_outside_main_thread(monkeypatch):
    main = AsyncMock()
    monkeypatch.setattr(listener, "main", main)
    errors = []

    def run():
        try:
            listener.run_bridge_server("us-east-1", "default", "app", "dev")
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert not errors
    main.assert_awaited_once()


def test_bridge_runs_when_loop_has_no_add_reader(monkeypatch):
    loop = asyncio.new_event_loop()
    monkeypatch.setattr(asyncio.events, "new_event_loop", lambda: loop)
    monkeypatch.setattr(loop, "add_reader", MagicMock(side_effect=NotImplementedError))
    main = AsyncMock()
    monkeypatch.setattr(listener, "main", main)

    listener.run_bridge_server("us-east-1", "default", "app", "dev")

    main.assert_awaited_once()
    assert loop.is_closed()
