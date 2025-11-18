import asyncio
import contextlib
import json
import sys
import uuid
from typing import ClassVar, final

import boto3
from awscrt import auth, mqtt
from awsiot import mqtt_connection_builder

# from rich.console import Console

# from stelvio.tunnel.ws import WebsocketClient


@final
class WebsocketHandlers:
    _handlers: ClassVar[list[callable]] = []

    @classmethod
    def register(cls, handler: callable) -> None:
        cls._handlers.append(handler)

    @classmethod
    async def handle_message(cls, data: any, client: "WebsocketClient") -> None:
        for handler in cls._handlers:
            await handler(data, client)

    @classmethod
    def all(cls) -> list[callable]:
        return cls._handlers


class WebsocketClient:
    def __init__(self, endpoint: str, region: str = "us-east-1", channel_id: str | None = None):
        """
        Initialize WebSocket client for AWS IoT Core.

        Args:
            endpoint: AWS IoT endpoint (e.g., "xyz-ats.iot.us-east-1.amazonaws.com")
            region: AWS region (default: "us-east-1")
            channel_id: Optional channel ID to subscribe to. If not provided, generates a random one.
        """
        # Strip the wss:// prefix and /mqtt suffix if present
        endpoint = endpoint.removeprefix("wss://")
        endpoint = endpoint.removesuffix("/mqtt")

        self.endpoint = endpoint
        self.region = region
        self.channel_id = channel_id or f"dev-{uuid.uuid4().hex[:8]}"
        self.client_id = f"stlv-dev-{uuid.uuid4().hex[:6]}"
        self.mqtt_connection = None
        self.message_queue = asyncio.Queue()
        self._event_loop = None

    def register_handler(self, handler: callable) -> None:
        WebsocketHandlers.register(handler)

    def _get_aws_credentials(self):
        """Get AWS credentials from the current environment."""
        # Use boto3 to get credentials (respects AWS_PROFILE, env vars, etc.)
        session = boto3.Session(region_name=self.region)
        credentials = session.get_credentials()

        if not credentials:
            raise ValueError(
                "No AWS credentials found. Please configure AWS credentials using:\n"
                "  - AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables\n"
                "  - AWS_PROFILE environment variable\n"
                "  - AWS credentials file (~/.aws/credentials)\n"
                "  - IAM role (if running on EC2/ECS/Lambda)"
            )

        frozen_creds = credentials.get_frozen_credentials()
        return {
            "access_key_id": frozen_creds.access_key,
            "secret_access_key": frozen_creds.secret_key,
            "session_token": frozen_creds.token,
        }

    def _on_message(self, topic, payload, **kwargs) -> None:
        """Callback for incoming MQTT messages."""
        # print(f"[dim]Message payload: {payload}[/dim]")
        try:
            message = payload.decode("utf-8")
            data = json.loads(message)
            # Schedule the coroutine on the main event loop from this thread
            if self._event_loop:
                asyncio.run_coroutine_threadsafe(self._process_message(data), self._event_loop)
            else:
                self._event_loop = asyncio.get_event_loop()
                asyncio.run_coroutine_threadsafe(self._process_message(data), self._event_loop)

        except json.JSONDecodeError:
            pass
        except Exception:
            pass

    async def _process_message(self, data: dict) -> None:
        """Process incoming message through registered handlers."""
        try:
            tasks = [
                asyncio.create_task(handler(data, self, self))
                for handler in WebsocketHandlers.all()
            ]
            if tasks:
                await asyncio.gather(*tasks)
        except Exception:
            # Stack trace
            import traceback

            traceback.print_exc()

    async def connect(self) -> None:
        """Connect to AWS IoT Core via MQTT over WebSocket."""
        try:
            # Store the event loop for cross-thread coroutine scheduling
            self._event_loop = asyncio.get_running_loop()

            # Get AWS credentials
            creds = self._get_aws_credentials()

            # Create credentials provider
            credentials_provider = auth.AwsCredentialsProvider.new_static(
                access_key_id=creds["access_key_id"],
                secret_access_key=creds["secret_access_key"],
                session_token=creds["session_token"],
            )

            # Build MQTT connection

            self.mqtt_connection = mqtt_connection_builder.websockets_with_default_aws_signing(
                endpoint=self.endpoint,
                region=self.region,
                credentials_provider=credentials_provider,
                on_connection_interrupted=self._on_connection_interrupted,
                on_connection_resumed=self._on_connection_resumed,
                client_id=self.client_id,
                clean_session=True,
                keep_alive_secs=30,
            )

            # Connect
            connect_future = self.mqtt_connection.connect()
            connect_future.result()

            # Subscribe to topic
            topic = f"public/{self.channel_id}"

            subscribe_future, _ = self.mqtt_connection.subscribe(
                topic=topic,
                qos=mqtt.QoS.AT_MOST_ONCE,
                callback=self._on_message,
            )
            subscribe_future.result()


            # Keep the connection alive
            try:
                # Run forever until interrupted
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                pass

        except KeyboardInterrupt:
            sys.exit(0)
        except Exception:
            sys.exit(1)
        finally:
            if self.mqtt_connection:
                disconnect_future = self.mqtt_connection.disconnect()
                disconnect_future.result()

    def _on_connection_interrupted(self, connection, error, **kwargs) -> None:
        """Callback when connection is interrupted."""

    def _on_connection_resumed(self, connection, return_code, session_present, **kwargs) -> None:
        """Callback when connection is resumed."""

    # Tunnel: Step 3a: Send JSON messages back to the tunnel service
    async def send_json(self, data: dict) -> None:
        """Publish a JSON message to AWS IoT topic."""
        if not self.mqtt_connection:
            raise RuntimeError("Not connected to AWS IoT")

        topic = f"public/{self.channel_id}"
        payload = json.dumps(data)

        # Publish is synchronous in the AWS IoT SDK, wrap in executor
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self.mqtt_connection.publish(
                topic=topic,
                payload=payload,
                qos=mqtt.QoS.AT_MOST_ONCE,
            ),
        )
        # publish_future.result()


async def listen_for_messages(client: WebsocketClient, duration: int = 10) -> dict | None:
    """
    Listen for incoming messages for a specified duration.
    Returns after the first message is received.

    Args:
        client: WebsocketClient instance
        duration: Maximum time to listen in seconds (default: 10)

    Returns:
        The received message as a dict, or None if timeout occurs
    """

    # Event to signal when a message is received
    message_received = asyncio.Event()
    received_data = None

    # Simple message handler that prints to console and signals completion
    async def message_handler(data: dict, client: WebsocketClient, logger) -> None:
        nonlocal received_data
        received_data = data
        message_received.set()

    # Register the handler
    client.register_handler(message_handler)

    # Start connection in background
    connection_task = asyncio.create_task(client.connect())

    # Wait for either a message or timeout
    try:
        await asyncio.wait_for(message_received.wait(), timeout=duration)
        return received_data
    except TimeoutError:
        return None
    finally:
        # Cancel the connection task to stop listening
        connection_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await connection_task


def main() -> None:
    # Example IoT endpoint - this would typically come from configuration or CLI args
    # Format: "your-iot-endpoint-ats.iot.region.amazonaws.com"
    endpoint = (
        sys.argv[1] if len(sys.argv) > 1 else "xyz-ats.iot.us-east-1.amazonaws.com"
    )
    region = sys.argv[2] if len(sys.argv) > 2 else "us-east-1"
    # channel_id = sys.argv[3] if len(sys.argv) > 3 else None
    channel_id = "dev-test"  # --- IGNORE ---

    # Create WebSocket client
    client = WebsocketClient(endpoint=endpoint, region=region, channel_id=channel_id)

    # Run the async listener with a 10-second timeout
    try:
        received_message = asyncio.run(listen_for_messages(client, duration=10))
        if received_message:
            pass
        else:
            pass
    except KeyboardInterrupt:
        pass
    except Exception:
        raise


if __name__ == "__main__":
    main()
