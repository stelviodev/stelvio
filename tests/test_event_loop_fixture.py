import subprocess
import sys


def test_event_loop_fixture_closes_pulumi_worker_loops(tmp_path):
    # Run the real fixture in a separate pytest session so checks happen after teardown.
    (tmp_path / "conftest.py").write_text("from tests.conftest import _event_loop\n")
    (tmp_path / "test_worker.py").write_text(
        """
import asyncio
import threading

from pulumi.runtime.sync_await import _ensure_event_loop
from pytest import mark

workers = []

@mark.parametrize("iteration", range(3))
def test_worker_cleanup(iteration):
    # Retain references so garbage collection cannot conceal missing cleanup.
    for main_loop, worker_loop, thread in workers:
        assert main_loop.is_closed()
        assert worker_loop.is_closed()
        assert not thread.is_alive()

    main_loop = asyncio.get_event_loop()
    def create_worker_loop():
        return _ensure_event_loop(), threading.current_thread()

    worker_loop, thread = main_loop.run_until_complete(
        main_loop.run_in_executor(None, create_worker_loop)
    )
    workers.append((main_loop, worker_loop, thread))
"""
    )
    result = subprocess.run(  # noqa: S603 — fixed interpreter and generated test only
        [sys.executable, "-m", "pytest", str(tmp_path), f"--rootdir={tmp_path}", "-q"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
