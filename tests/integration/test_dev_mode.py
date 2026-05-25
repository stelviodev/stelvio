import asyncio
import json
import threading
import time
import urllib.request

import pytest

from stelvio.aws.function import Function
from stelvio.bridge.local.listener import main as bridge_main

from .export_helpers import export_function

pytestmark = pytest.mark.integration


class _BridgeRunner:
    """Run the local bridge listener in a background thread."""

    def __init__(self, *, region: str, profile: str | None, app_name: str, env: str):
        self._coro_kwargs = {
            "region": region,
            "profile": profile,
            "app_name": app_name,
            "env": env,
        }
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        def _runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._task = loop.create_task(bridge_main(**self._coro_kwargs))
            self._ready.set()
            try:
                loop.run_until_complete(self._task)
            except asyncio.CancelledError:
                pass
            finally:
                loop.close()

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    def stop(self) -> None:
        if self._loop and self._task and not self._task.done():
            self._loop.call_soon_threadsafe(self._task.cancel)
        if self._thread:
            self._thread.join(timeout=10)


def test_function_url_dev_mode(stelvio_env, project_dir):
    """Dev mode routes Function URL invocations to a locally-running handler.

    The deployed Lambda is just the Stelvio bridge stub; the real handler code
    runs in this test process. We prove local execution by setting an env var
    that exists only locally and asserting it round-trips through the URL.
    """
    marker = f"local-{stelvio_env.run_id}"

    def infra():
        fn = Function(
            "dev-url",
            handler="handlers/dev_echo.main",
            url="public",
            environment={"DEV_TEST_MARKER": marker},
        )
        export_function(fn)

    outputs = stelvio_env.deploy(infra, dev_mode=True)

    url = outputs["function_dev-url_url"]
    assert url.startswith("https://")

    bridge = _BridgeRunner(
        region=stelvio_env.aws_region,
        profile=stelvio_env.aws_profile,
        app_name=f"stlv-{stelvio_env.run_id}",
        env="test",
    )
    bridge.start()

    try:
        # Give the listener time to connect and subscribe to AppSync
        time.sleep(8)

        with urllib.request.urlopen(url, timeout=90) as resp:  # noqa: S310
            assert resp.status == 200
            payload = json.loads(resp.read().decode())

        assert payload["marker"] == marker
    finally:
        bridge.stop()
