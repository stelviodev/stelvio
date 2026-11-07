"""A real Click exit while a Function's production executor worker remains blocked."""

import asyncio
import importlib
import json
import selectors
import signal
import sys
from pathlib import Path
from threading import Event, get_ident

import click

from stelvio.aws.function.config import FunctionConfig
from stelvio.aws.function.function import Function
from stelvio.bridge.local import listener
from stelvio.bridge.remote.infrastructure import AppSyncResource

handler_started = Event()
interrupt_phase = sys.argv[1]
project_root = Path(sys.argv[2])
bridge_loops = []


class WebSocket:
    def __aiter__(self):
        return self

    async def __anext__(self):
        return json.dumps(
            {
                "type": "data",
                "event": json.dumps(
                    {
                        "endpointId": "endpoint-1",
                        "event": {},
                        "context": {
                            "invoke_id": "request-1",
                            "client_context": None,
                            "cognito_identity": None,
                            "epoch_deadline_time_in_ms": 0,
                        },
                    }
                ),
            }
        )

    async def close(self):
        await asyncio.Future()


def blocked_handler(_event, _context):
    print("HANDLER_STARTED", flush=True)
    handler_started.set()
    if interrupt_phase == "worker":
        bridge_loops[0].call_soon_threadsafe(lambda: None)
        assert sys.stdin.buffer.read(1), "Parent did not release worker signal"
        signal.pthread_kill(get_ident(), signal.SIGINT)
    Event().wait()


async def connect(_config):
    bridge_loops.append(asyncio.get_running_loop())
    return WebSocket()


async def handler_environment():
    return {}


@click.command()
def run_bridge():
    listener.run_bridge_server("us-east-1", "default", "app", "dev")


def announce_selector_wait():
    select = selectors.DefaultSelector.select
    announced = False

    def select_when_ready(self, timeout=None):
        nonlocal announced
        if handler_started.is_set() and not announced:
            announced = True
            print("LOOP_WAITING", flush=True)
        return select(self, timeout)

    selectors.DefaultSelector.select = select_when_ready


if __name__ == "__main__":
    if interrupt_phase == "worker":
        announce_selector_wait()
    listener.discover_or_create_appsync = lambda *_args: AppSyncResource(
        api_id="api-id",
        http_endpoint="https://example.com",
        realtime_endpoint="realtime.example.com",
        api_key="api-key",
    )
    listener.connect_to_appsync = connect
    listener.subscribe_to_channel = lambda *_args: asyncio.sleep(0)

    # Bypass deployment plumbing, exercising the real handler import, context and executor.
    project_root.joinpath("blocked.py").write_text(
        "from __main__ import blocked_handler as main\n"
    )
    function_module = importlib.import_module("stelvio.aws.function.function")
    function_module.get_project_root = lambda: project_root
    function = object.__new__(Function)
    function._config = FunctionConfig(handler="blocked.main")
    function._dev_endpoint_id = "endpoint-1"
    function._stlv_resources_code = None
    function._get_environment_for_bridge_event = handler_environment
    listener.WebsocketHandlers.all = lambda: [function]
    cli_module = importlib.import_module("stelvio.cli")
    cli_module.cli = run_bridge
    sys.argv[:] = ["stlv"]
    cli_module.main()
