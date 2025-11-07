#!/usr/bin/env python3
"""
WebSocket client for stlv-tunnel

Connects to a WebSocket server at a specific path and pretty-prints
all received messages in JSON format. Automatically responds to requests
with type "request-received" by sending back a "request-processed" message.

Usage:
    uv run python ws.py <ws_url>
    
Example:
    uv run python ws.py ws://localhost:8787/mypath
"""

import asyncio
import json
import os
import random
import string
import sys
from typing import Optional, final

# from .functions.api import handler_real

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

import websockets



async def example_handler(data, client: 'WebsocketClient'):
    print("🔍 Example handler invoked.")


@final
class WebsocketClient:
    def __init__(self, url: str):
        self.url = url
        self.handlers = []
        self.register_handler(example_handler)

    def register_handler(self, handler):
        self.handlers.append(handler)

    async def connect(self):
        url = self.url
        print(f"🔌 Connecting to {url}...", flush=True)
        try:
            async with websockets.connect(url) as websocket:
                print(f"✅ Connected to {url}", flush=True)
                print("📡 Listening for messages and auto-responding to requests...\n", flush=True)

                async for message in websocket:
                    print("=" * 80, flush=True)
                    print("📨 Received message:", flush=True)
                    print("-" * 80, flush=True)

                    try:
                        # Try to parse as JSON and pretty-print
                        data = json.loads(message)

                        # tasks = []
                        # for handler in self.handlers:
                        #     # Launch each handler without blocking so they can run in parallel.
                        #     tasks.append(asyncio.create_task(handler(data, self)))

                        # if tasks:
                        #     await asyncio.gather(*tasks)

                        print(json.dumps(data, indent=2, sort_keys=True), flush=True)

                        # Check if this is a request that needs a response
                        if data.get("type") == "request-received" and "requestId" in data:
                            request_id = data["requestId"]

                            # Generate a random response
                            # random_response = generate_random_string(20)
                            # random_response = input("Enter response: ")
                            random_response = handler_real({}, {})

                            # Create response message
                            response_message = {
                                "payload": random_response,
                                "requestId": request_id,
                                "type": "request-processed"
                            }

                            # Send response back
                            await websocket.send(json.dumps(response_message))

                            print("-" * 80, flush=True)
                            print("📤 Sent response:", flush=True)
                            print(json.dumps(response_message, indent=2, sort_keys=True), flush=True)
                    except json.JSONDecodeError:
                        print("❌ Failed to decode JSON:", flush=True)
                        print(message, flush=True)
        except ConnectionRefusedError:
            print(f"❌ Connection refused. Is the server running at {url}?", file=sys.stderr, flush=True)
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n👋 Disconnected by user", flush=True)
            sys.exit(0)




def handler_real(*args, **kwargs):
    # Import the `handler_real` function from file `functions/api.py` using importlib
    script_path = os.path.dirname(os.path.abspath(__file__))
    from importlib import util
    spec = util.spec_from_file_location("api", f"{script_path}/functions/api.py")
    api = util.module_from_spec(spec)
    spec.loader.exec_module(api)
    handler_real = api.handler_real
    return handler_real(*args, **kwargs)



def generate_random_string(length: int = 16) -> str:
    """Generate a random string of specified length."""
    characters = string.ascii_letters + string.digits
    return ''.join(random.choice(characters) for _ in range(length))



def main(url):
    """Main entry point."""    
    # Validate URL scheme
    if not url.startswith(("ws://", "wss://")):
        print("❌ Error: URL must start with ws:// or wss://", file=sys.stderr, flush=True)
        sys.exit(1)
    
    # Run the async connection
    client = WebsocketClient(url)
    asyncio.run(client.connect())


if __name__ == "__main__":
    if not sys.argv[1:]:
        sys.argv.append("wss://stlv-tunnel.contact-c10.workers.dev/demo")
    main(sys.argv[1])
