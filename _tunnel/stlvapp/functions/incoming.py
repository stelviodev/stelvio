

import json
import boto3
import os
import uuid
import time


import asyncio
import contextlib
import json
import sys
import uuid
from typing import ClassVar, final

import boto3
# from awscrt import auth, mqtt
# from awsiot import mqtt_connection_builder

import websocket

# from rich.console import Console

# from stelvio.tunnel.ws import WebsocketClient


# @final
# class WebsocketHandlers:
#     _handlers: ClassVar[list[callable]] = []

#     @classmethod
#     def register(cls, handler: callable) -> None:
#         cls._handlers.append(handler)

#     @classmethod
#     async def handle_message(cls, data: any, client: "WebsocketClient") -> None:
#         for handler in cls._handlers:
#             await handler(data, client)

#     @classmethod
#     def all(cls) -> list[callable]:
#         return cls._handlers


# class WebsocketClient:
#     def __init__(self, endpoint: str, region: str = "us-east-1", channel_id: str | None = None):
#         """
#         Initialize WebSocket client for AWS IoT Core.

#         Args:
#             endpoint: AWS IoT endpoint (e.g., "a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com")
#             region: AWS region (default: "us-east-1")
#             channel_id: Optional channel ID to subscribe to. If not provided, generates a random one.
#         """
#         # Strip the wss:// prefix and /mqtt suffix if present
#         endpoint = endpoint.removeprefix("wss://")
#         endpoint = endpoint.removesuffix("/mqtt")

#         self.endpoint = endpoint
#         self.region = region
#         self.channel_id = channel_id or f"dev-{uuid.uuid4().hex[:8]}"
#         self.client_id = f"stlv-dev-{uuid.uuid4().hex[:6]}"
#         self.mqtt_connection = None
#         self.message_queue = asyncio.Queue()
#         self._event_loop = None

#     def register_handler(self, handler: callable) -> None:
#         WebsocketHandlers.register(handler)

#     def _get_aws_credentials(self):
#         """Get AWS credentials from the current environment."""
#         # Use boto3 to get credentials (respects AWS_PROFILE, env vars, etc.)
#         session = boto3.Session(region_name=self.region)
#         credentials = session.get_credentials()

#         if not credentials:
#             raise ValueError(
#                 "No AWS credentials found. Please configure AWS credentials using:\n"
#                 "  - AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY environment variables\n"
#                 "  - AWS_PROFILE environment variable\n"
#                 "  - AWS credentials file (~/.aws/credentials)\n"
#                 "  - IAM role (if running on EC2/ECS/Lambda)"
#             )

#         frozen_creds = credentials.get_frozen_credentials()
#         return {
#             "access_key_id": frozen_creds.access_key,
#             "secret_access_key": frozen_creds.secret_key,
#             "session_token": frozen_creds.token,
#         }

#     def _on_message(self, topic, payload, **kwargs) -> None:
#         """Callback for incoming MQTT messages."""
#         # print(f"[dim]Message payload: {payload}[/dim]")
#         try:
#             message = payload.decode("utf-8")
#             data = json.loads(message)
#             # Schedule the coroutine on the main event loop from this thread
#             if self._event_loop:
#                 asyncio.run_coroutine_threadsafe(self._process_message(data), self._event_loop)
#             else:
#                 self._event_loop = asyncio.get_event_loop()
#                 asyncio.run_coroutine_threadsafe(self._process_message(data), self._event_loop)

#         except json.JSONDecodeError:
#             pass
#         except Exception:
#             pass

#     async def _process_message(self, data: dict) -> None:
#         """Process incoming message through registered handlers."""
#         try:
#             tasks = [
#                 asyncio.create_task(handler(data, self, self))
#                 for handler in WebsocketHandlers.all()
#             ]
#             if tasks:
#                 await asyncio.gather(*tasks)
#         except Exception:
#             # Stack trace
#             import traceback

#             traceback.print_exc()

#     async def connect(self) -> None:
#         """Connect to AWS IoT Core via MQTT over WebSocket."""
#         try:
#             # Store the event loop for cross-thread coroutine scheduling
#             self._event_loop = asyncio.get_running_loop()

#             # Get AWS credentials
#             creds = self._get_aws_credentials()

#             # Create credentials provider
#             credentials_provider = auth.AwsCredentialsProvider.new_static(
#                 access_key_id=creds["access_key_id"],
#                 secret_access_key=creds["secret_access_key"],
#                 session_token=creds["session_token"],
#             )

#             # Build MQTT connection

#             self.mqtt_connection = mqtt_connection_builder.websockets_with_default_aws_signing(
#                 endpoint=self.endpoint,
#                 region=self.region,
#                 credentials_provider=credentials_provider,
#                 on_connection_interrupted=self._on_connection_interrupted,
#                 on_connection_resumed=self._on_connection_resumed,
#                 client_id=self.client_id,
#                 clean_session=True,
#                 keep_alive_secs=30,
#             )

#             # Connect
#             connect_future = self.mqtt_connection.connect()
#             connect_future.result()

#             # Subscribe to topic
#             topic = f"public/{self.channel_id}"

#             subscribe_future, _ = self.mqtt_connection.subscribe(
#                 topic=topic,
#                 qos=mqtt.QoS.AT_MOST_ONCE,
#                 callback=self._on_message,
#             )
#             subscribe_future.result()


#             # Keep the connection alive
#             try:
#                 # Run forever until interrupted
#                 while True:
#                     await asyncio.sleep(1)
#             except KeyboardInterrupt:
#                 pass

#         except KeyboardInterrupt:
#             sys.exit(0)
#         except Exception:
#             sys.exit(1)
#         finally:
#             if self.mqtt_connection:
#                 disconnect_future = self.mqtt_connection.disconnect()
#                 disconnect_future.result()

#     def _on_connection_interrupted(self, connection, error, **kwargs) -> None:
#         """Callback when connection is interrupted."""

#     def _on_connection_resumed(self, connection, return_code, session_present, **kwargs) -> None:
#         """Callback when connection is resumed."""

#     # Tunnel: Step 3a: Send JSON messages back to the tunnel service
#     async def send_json(self, data: dict) -> None:
#         """Publish a JSON message to AWS IoT topic."""
#         if not self.mqtt_connection:
#             raise RuntimeError("Not connected to AWS IoT")

#         topic = f"public/{self.channel_id}"
#         payload = json.dumps(data)

#         # Publish is synchronous in the AWS IoT SDK, wrap in executor
#         loop = asyncio.get_event_loop()
#         await loop.run_in_executor(
#             None,
#             lambda: self.mqtt_connection.publish(
#                 topic=topic,
#                 payload=payload,
#                 qos=mqtt.QoS.AT_MOST_ONCE,
#             ),
#         )
#         # publish_future.result()


# async def listen_for_messages(client: WebsocketClient, duration: int = 10) -> dict | None:
#     """
#     Listen for incoming messages for a specified duration.
#     Returns after the first message is received.

#     Args:
#         client: WebsocketClient instance
#         duration: Maximum time to listen in seconds (default: 10)

#     Returns:
#         The received message as a dict, or None if timeout occurs
#     """

#     # Event to signal when a message is received
#     message_received = asyncio.Event()
#     received_data = None

#     # Simple message handler that prints to console and signals completion
#     async def message_handler(data: dict, client: WebsocketClient, logger) -> None:
#         nonlocal received_data
#         received_data = data
#         message_received.set()

#     # Register the handler
#     client.register_handler(message_handler)

#     # Start connection in background
#     connection_task = asyncio.create_task(client.connect())

#     # Wait for either a message or timeout
#     try:
#         await asyncio.wait_for(message_received.wait(), timeout=duration)
#         return received_data
#     except TimeoutError:
#         return None
#     finally:
#         # Cancel the connection task to stop listening
#         connection_task.cancel()
#         with contextlib.suppress(asyncio.CancelledError):
#             await connection_task


# def main() -> None:
#     # Example IoT endpoint - this would typically come from configuration or CLI args
#     # Format: "your-iot-endpoint-ats.iot.region.amazonaws.com"
#     endpoint = (
#         sys.argv[1] if len(sys.argv) > 1 else "a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com"
#     )
#     region = sys.argv[2] if len(sys.argv) > 2 else "us-east-1"
#     # channel_id = sys.argv[3] if len(sys.argv) > 3 else None
#     channel_id = "dev-test"  # --- IGNORE ---

#     # Create WebSocket client
#     client = WebsocketClient(endpoint=endpoint, region=region, channel_id=channel_id)

#     # Run the async listener with a 10-second timeout
#     try:
#         received_message = asyncio.run(listen_for_messages(client, duration=10))
#         if received_message:
#             pass
#         else:
#             pass
#     except KeyboardInterrupt:
#         pass
#     except Exception:
#         raise


# if __name__ == "__main__":
#     main()




# def wait_for_response(iot_client, channel_id, request_id, timeout=10):
#     """
#     Wait for a response message by polling IoT Thing Shadow.
#     The tunnel service should update the shadow with the response.
#     """
#     thing_name = f"{channel_id}"
#     start_time = time.time()
#     poll_interval = 0.1  # Poll every 100ms
    
#     c = 0
#     last_shadow_payload = None
#     while time.time() - start_time < timeout:

#         try:
#             response = iot_client.get_thing_shadow(thingName=thing_name)
#         except Exception as e:
#             return {"message": "Error polling shadow", "error": str(e)}

#         try:
#             # Get the thing shadow
#             response = iot_client.get_thing_shadow(thingName=thing_name)
#             shadow_payload = json.loads(response['payload'].read())
#             last_shadow_payload = shadow_payload
            
#             # Check if shadow contains our response
#             if 'state' in shadow_payload and 'reported' in shadow_payload['state']:
#                 reported = shadow_payload['state']['reported']
#                 if reported.get('requestId') == request_id and reported.get('type') == 'request-processed':
#                     # Found matching response
#                     return reported.get('payload', {})
#         except iot_client.exceptions.ResourceNotFoundException:
#             # Shadow doesn't exist yet, continue polling
#             # return {"message": "Shadow not found"}
#             c += 1
#             if c > 20:
#                 return {"message": f"Shadow not found after {c} attempts, last payload: {last_shadow_payload}"}
#         except Exception as e:
#             # Log error but continue polling
#             return {"message": "Error polling shadow", "error": str(e)}
        
#         time.sleep(poll_interval)
    
#     # Timeout reached
#     raise TimeoutError(f"No response received within {timeout} seconds")


# def handler(event, context):

#     return {
#         "statusCode": 200,
#         "body": json.dumps({"message": "incoming.py l:337"}),
#     }

#     channel_id = event["pathParameters"]["channel_id"]
#     incoming_post_data = json.loads(event.get("body", "{}"))

#     # Get IoT endpoint from environment or use default
#     iot_endpoint = os.environ.get("IOT_ENDPOINT", "a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com")
#     wss_url = f"wss://{iot_endpoint}/mqtt"
    
#     # Publish to IoT topic
#     topic = f"public/{channel_id}"


    
#     try:
#         # Create IoT Data client
#         iot_client = boto3.client('iot-data', endpoint_url=f"https://{iot_endpoint}")

#         # Create a thing shadow name based on channel ID
#         thing_name = f"tunnel-{channel_id}"  # --- IGNORE --
        


#         # const wrappedMessage = JSON.stringify({
# 		# 			payload: JSON.parse(payload),
# 		# 			requestId: requestId,
# 		# 			type: "request-received"
# 		# 		});

#         wrapped_message = {
#             "payload": incoming_post_data,
#             "type": "request-received",
#             "requestId": uuid.uuid4().hex
#         }

#         main()
        
#         # Publish the incoming POST data to the IoT topic
#         response = iot_client.publish(
#             topic=topic,
#             qos=0,  # QoS 0 = AT_MOST_ONCE
#             payload=json.dumps(wrapped_message)
#         )

#         # Wait for response from tunnel service
#         try:
#             response_payload = wait_for_response(iot_client, channel_id, wrapped_message["requestId"], timeout=10)
            
#             # Clean up the shadow after receiving response
#             thing_name = f"tunnel-{channel_id}"
#             try:
#                 iot_client.delete_thing_shadow(thingName=thing_name)
#             except:
#                 pass  # Ignore cleanup errors
            
#             return {
#                 "statusCode": 200,
#                 "body": json.dumps({
#                     "response": response_payload,
#                     "l": 391,
#                 })
#             }
#         except TimeoutError:
#             return {
#                 "statusCode": 500,
#                 "body": json.dumps({
#                     "response": "Timeout"
#                 })
#             }
#     except Exception as e:
#         return {
#             "statusCode": 500,
#             "body": json.dumps({
#                 "error": str(e),
#                 "channel_id": channel_id,
#             })
#         }



def handler2(event, context):
    post_data = json.loads(event.get("body", "{}"))
    channel_id = event["pathParameters"]["channel_id"]
    iot_endpoint = os.environ.get("IOT_ENDPOINT", "a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com")
    wss_url = f"wss://{iot_endpoint}/mqtt"
    topic = f"public/{channel_id}"

    wrapped_message = {
        "payload": post_data,
        "type": "request-received",
        "requestId": uuid.uuid4().hex
    }

    iot_client = boto3.client('iot-data', endpoint_url=f"https://{iot_endpoint}")


    # Publish the incoming POST data to the IoT topic
    response = iot_client.publish(
        topic=topic,
        qos=0,  # QoS 0 = AT_MOST_ONCE
        payload=json.dumps(wrapped_message)
    )

    session = boto3.Session()
    creds = session.get_credentials().get_frozen_credentials()


    ENDPOINT = "a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com"
    REGION = "us-east-1"
    TOPIC = "public/dev-test"
    msg = connect_and_get_first_message(ENDPOINT, REGION, TOPIC)

    


    return {
        "statusCode": 200,
        "body": json.dumps({"message": "handler2 called", 
        # "post_data": post_data, 
        # "channel_id": channel_id,
        # "iot_endpoint": iot_endpoint,
        # "wss_url": wss_url,
        # "topic": topic,
        # "wrapped_message": wrapped_message,
        # "TOKEN": os.environ.get("TOKEN"),
        # "IOT_ENDPOINT": os.environ.get("IOT_ENDPOINT"),
        # "ACCESS_KEY": os.environ.get("ACCESS_KEY"),
        # "SECRET_KEY": os.environ.get("SECRET_KEY"),
        # "AWS_ACCESS_KEY_ID": creds.access_key,
        # "AWS_SECRET_ACCESS_KEY": creds.secret_key,
        # "AWS_SESSION_TOKEN": creds.token,
        "msg": msg,

        }),
    }


#####################################################
# 
# 
# 
# 
# ###################################################
# 
# 
#!/usr/bin/env python3
"""
MQTT over WebSockets (SigV4) to AWS IoT Core using websocket-client + boto3.

Requirements:
    pip install websocket-client boto3

This script:
 - Connects to AWS IoT via WebSocket
 - Subscribes to a topic
 - Returns the first message received, or times out
"""

import boto3
import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.parse
from websocket import WebSocketApp


class TimeoutException(Exception):
    pass


def sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def get_signature_key(key, date_stamp, region, service):
    k_date = sign(("AWS4" + key).encode("utf-8"), date_stamp)
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
    return k_signing


def build_sigv4_ws_url(endpoint, region, access_key, secret_key, session_token=None):
    method = "GET"
    service = "iotdevicegateway"
    host = endpoint
    canonical_uri = "/mqtt"

    t = time.gmtime()
    amz_date = time.strftime("%Y%m%dT%H%M%SZ", t)
    date_stamp = time.strftime("%Y%m%d", t)

    canonical_querystring = (
        "X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Credential={urllib.parse.quote(access_key + '/' + date_stamp + '/' + region + '/' + service + '/aws4_request', safe='')}"
        f"&X-Amz-Date={amz_date}"
        "&X-Amz-SignedHeaders=host"
    )

    if session_token:
        canonical_querystring += f"&X-Amz-Security-Token={urllib.parse.quote(session_token, safe='')}"

    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = hashlib.sha256("".encode("utf-8")).hexdigest()

    canonical_request = (
        f"{method}\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )

    algorithm = "AWS4-HMAC-SHA256"
    string_to_sign = (
        f"{algorithm}\n{amz_date}\n{date_stamp}/{region}/{service}/aws4_request\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    signing_key = get_signature_key(secret_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    canonical_querystring += f"&X-Amz-Signature={signature}"

    return f"wss://{host}{canonical_uri}?{canonical_querystring}"


def connect_and_get_first_message(endpoint, region, topic, timeout_seconds=30):
    """
    Connects via WebSocket MQTT, subscribes to `topic`,
    returns the first message or raises TimeoutException.
    """

    # session = boto3.Session()
    # creds = session.get_credentials().get_frozen_credentials()
    # print(f"{creds=}")

    url = build_sigv4_ws_url(
        endpoint=endpoint,
        region=region,
        access_key=os.environ.get("ACCESS_KEY"),
        secret_key=os.environ.get("SECRET_KEY"),
        session_token=os.environ.get("TOKEN"),
    )

    received_message = {"data": None}
    done_event = threading.Event()

    def on_open(ws):
        print(f"WebSocket connected, sending MQTT CONNECT")
        
        # First send MQTT CONNECT packet
        client_id = f"python-client-{int(time.time())}"
        client_id_bytes = client_id.encode("utf-8")
        
        # MQTT CONNECT packet (MQTT 3.1.1)
        connect_packet = bytearray()
        connect_packet.append(0x10)  # CONNECT packet type
        
        # Variable header
        protocol_name = b"\x00\x04MQTT"  # Protocol name length + "MQTT"
        protocol_level = b"\x04"  # MQTT 3.1.1
        connect_flags = b"\x02"  # Clean session
        keep_alive = b"\x00\x3c"  # 60 seconds
        
        # Payload (client ID)
        payload = len(client_id_bytes).to_bytes(2, "big") + client_id_bytes
        
        variable_header = protocol_name + protocol_level + connect_flags + keep_alive
        remaining_length = len(variable_header) + len(payload)
        
        connect_packet.extend(encode_mqtt_length(remaining_length))
        connect_packet.extend(variable_header)
        connect_packet.extend(payload)
        
        ws.send(connect_packet, opcode=0x02)  # binary opcode
        
        # Wait a bit for CONNACK, then subscribe
        time.sleep(0.5)
        
        print(f"Subscribing to {topic}")
        # Send MQTT SUBSCRIBE packet (MQTT 3.1.1)
        packet_id = 1
        packet = bytearray()

        # Fixed header
        packet.append(0x82)  # SUBSCRIBE
        variable_header = packet_id.to_bytes(2, "big")

        # Payload
        topic_bytes = topic.encode("utf-8")
        payload = (
            len(topic_bytes).to_bytes(2, "big")
            + topic_bytes
            + b"\x00"  # QoS 0
        )

        remaining_length = len(variable_header) + len(payload)
        packet.extend(encode_mqtt_length(remaining_length))
        packet.extend(variable_header)
        packet.extend(payload)

        ws.send(packet, opcode=0x02)  # binary opcode

    def on_message(ws, message):
        # Raw MQTT frame; parse minimal PUBLISH
        msg = try_parse_mqtt_publish(message)
        if msg is not None:
            received_message["data"] = msg
            done_event.set()

    def on_error(ws, err):
        # Only handle real errors, not close frames
        err_str = str(err)
        if "opcode=8" not in err_str:  # opcode 8 = close frame
            print(f"WebSocket error: {err}")
            received_message["error"] = err_str
            done_event.set()

    def on_close(ws, close_status_code, close_msg):
        print(f"WebSocket closed: {close_status_code} {close_msg}")
        # Only set event if we haven't received data yet
        if received_message["data"] is None and "error" not in received_message:
            done_event.set()

    ws = WebSocketApp(
        url,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
        header={"Sec-WebSocket-Protocol": "mqtt"},
    )

    thread = threading.Thread(target=ws.run_forever, daemon=True)
    thread.start()

    print(f"Waiting up to {timeout_seconds} seconds for a message...")
    # Wait for first message
    if not done_event.wait(timeout_seconds):
        ws.close()
        raise TimeoutException(f"No message received within {timeout_seconds} seconds.")

    ws.close()
    if "error" in received_message:
        raise Exception(f"WebSocket error: {received_message['error']}")
    return received_message["data"]


def encode_mqtt_length(length):
    """Encode MQTT Remaining Length field"""
    result = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length > 0:
            digit |= 0x80
        result.append(digit)
        if length == 0:
            break
    return result


def try_parse_mqtt_publish(frame):
    """
    Minimal MQTT PUBLISH frame parser, enough for JSON payloads.
    """
    if not frame:
        return None

    frame = bytearray(frame)
    packet_type = frame[0] >> 4
    if packet_type != 3:  # 3 = PUBLISH
        return None

    # Skip fixed header
    idx = 1

    # Remaining length (MQTT variable int)
    multiplier = 1
    remaining = 0
    while True:
        digit = frame[idx]
        idx += 1
        remaining += (digit & 127) * multiplier
        if (digit & 128) == 0:
            break
        multiplier *= 128

    # Topic length
    topic_len = int.from_bytes(frame[idx:idx+2], "big")
    idx += 2
    topic = frame[idx:idx + topic_len].decode()
    idx += topic_len

    # QoS check (ignore packet identifier if QoS1/2)
    # Assume QoS 0 → no packet ID

    payload = frame[idx:]
    try:
        return json.loads(payload.decode("utf-8"))
    except:
        return payload.decode("utf-8")


# -------------------
# Example usage
# -------------------
if __name__ == "__main__":
    ENDPOINT = "a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com"
    REGION = "us-east-1"
    TOPIC = "public/dev-test"

    try:
        msg = connect_and_get_first_message(ENDPOINT, REGION, TOPIC)
        print("Received:", msg)
    except TimeoutException as e:
        print("Timeout:", e)
    