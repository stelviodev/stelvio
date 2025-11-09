#!/usr/bin/env python3
"""
WebSocket client for stlv-tunnel

"""

import asyncio
import json
import sys
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar, final

import boto3
from awscrt import auth, mqtt
from awsiot import mqtt_connection_builder
from rich.console import Console

# Force unbuffered output
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

NOT_A_TEAPOT = 418

console = Console(soft_wrap=True)


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


class TunnelLogger(ABC):
    @abstractmethod
    def log(  # noqa: PLR0913
        self,
        protocol: str,
        method: str,
        path: str,
        source_ip: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        raise NotImplementedError


@final
class WebsocketClient(TunnelLogger):
    def __init__(self, endpoint: str, region: str = "us-east-1", channel_id: str | None = None):
        """
        Initialize WebSocket client for AWS IoT Core.
        
        Args:
            endpoint: AWS IoT endpoint (e.g., "a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com")
            region: AWS region (default: "us-east-1")
            channel_id: Optional channel ID to subscribe to. If not provided, generates a random one.
        """
        # Strip the wss:// prefix and /mqtt suffix if present
        if endpoint.startswith("wss://"):
            endpoint = endpoint[6:]
        if endpoint.endswith("/mqtt"):
            endpoint = endpoint[:-5]
            
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

    def _on_message(self, topic, payload, **kwargs):
        """Callback for incoming MQTT messages."""
        console.print(f"[dim]Message received on topic: {topic}[/dim]")
        # console.print(f"[dim]Message payload: {payload}[/dim]")
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
            console.print(f"[yellow]Warning: Received non-JSON message: {message}[/yellow]")
        except Exception as e:
            console.print(f"[red]Error processing message: {e}[/red]")

    async def _process_message(self, data: dict) -> None:
        """Process incoming message through registered handlers."""
        console.print(f"[dim]Processing message: {data}[/dim]")
        console.print(f"Registered handlers: {len(WebsocketHandlers.all())}")
        try:
            tasks = [
                asyncio.create_task(handler(data, self, self))
                for handler in WebsocketHandlers.all()
            ]
            if tasks:
                await asyncio.gather(*tasks)
        except Exception as e:
            console.print(f"[red]Error in message handler: {e}[/red]")
            # Stack trace
            import traceback
            traceback.print_exc()
            console.print(f"[dim]Error in message handler: {e}[/dim]")

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
            console.print(f"[dim]Connecting to AWS IoT endpoint: {self.endpoint}[/dim]")
            console.print(f"[dim]Client ID: {self.client_id}[/dim]")
            console.print(f"[dim]Channel ID: {self.channel_id}[/dim]")
            
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
            console.print(f"[green]✓ Connected to AWS IoT[/green]")
            
            # Subscribe to topic
            topic = f"public/{self.channel_id}"
            console.print(f"[dim]Subscribing to topic: {topic}[/dim]")
            
            subscribe_future, _ = self.mqtt_connection.subscribe(
                topic=topic,
                qos=mqtt.QoS.AT_MOST_ONCE,
                callback=self._on_message,
            )
            subscribe_future.result()
            console.print(f"[green]✓ Subscribed to {topic}[/green]")
            
            console.print("\n\n📡 [bold green]Ready to accept tunnel events...[/bold green]\n")
            console.print(f"[dim]Listening on channel: {self.channel_id}[/dim]\n\n")
            
            # Keep the connection alive
            try:
                # Run forever until interrupted
                while True:
                    await asyncio.sleep(1)
            except KeyboardInterrupt:
                console.print("\n[yellow]Shutting down...[/yellow]")
                
        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/yellow]")
            sys.exit(0)
        except Exception as e:
            console.print(f"[red]Connection error: {e}[/red]")
            sys.exit(1)
        finally:
            if self.mqtt_connection:
                console.print("[dim]Disconnecting...[/dim]")
                disconnect_future = self.mqtt_connection.disconnect()
                disconnect_future.result()
                console.print("[green]✓ Disconnected[/green]")

    def _on_connection_interrupted(self, connection, error, **kwargs):
        """Callback when connection is interrupted."""
        console.print(f"[yellow]⚠️  Connection interrupted: {error}[/yellow]")

    def _on_connection_resumed(self, connection, return_code, session_present, **kwargs):
        """Callback when connection is resumed."""
        console.print(f"[green]✓ Connection resumed[/green]")

    # Tunnel: Step 3a: Send JSON messages back to the tunnel service
    async def send_json(self, data: dict) -> None:
        """Publish a JSON message to AWS IoT topic."""
        if not self.mqtt_connection:
            raise RuntimeError("Not connected to AWS IoT")
        
        topic = f"public/{self.channel_id}"
        payload = json.dumps(data)
        
        # Publish is synchronous in the AWS IoT SDK, wrap in executor
        loop = asyncio.get_event_loop()
        publish_future = await loop.run_in_executor(
            None,
            lambda: self.mqtt_connection.publish(
                topic=topic,
                payload=payload,
                qos=mqtt.QoS.AT_MOST_ONCE,
            )
        )
        # publish_future.result()

    def log(  # noqa: PLR0913
        self,
        protocol: str,
        method: str,
        path: str,
        source_ip: str,
        status_code: int,
        duration_ms: float,
    ) -> None:
        if status_code == NOT_A_TEAPOT:
            status_code = "❌🫖"
        else:
            status_code = str(status_code)
            match status_code[0]:
                case "2":
                    status_color = "green"
                case "4":
                    status_color = "yellow"
                case "5":
                    status_color = "red"
                case _:
                    status_color = "white"
            status_code = f"[bold {status_color}]{status_code}[/bold {status_color}]"
        loop_time = asyncio.get_event_loop().time()
        wall_clock = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        timestamp = f"[grey][{wall_clock} : {loop_time:07.2f}][/grey]"
        console.print(
            f"{timestamp} [bold]{protocol}[/bold] [bold blue]{method}[/bold blue] "
            f"[cyan]{path}[/cyan] [grey]{source_ip}[/grey] {status_code} {duration_ms:07.2f}ms",
            highlight=False,
        )
