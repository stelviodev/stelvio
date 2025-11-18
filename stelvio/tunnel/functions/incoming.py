import hashlib
import hmac
import json
import os
import threading
import time
import urllib.parse
import uuid

import boto3
from websocket import WebSocketApp


def handler2(event, context):
    post_data = json.loads(event.get("body", "{}"))
    channel_id = event["pathParameters"]["channel_id"]
    iot_endpoint = os.environ.get("IOT_ENDPOINT", "")
    topic = f"public/{channel_id}"
    request_id = str(uuid.uuid4())

    wrapped_message = {"payload": post_data, "type": "request-received", "requestId": request_id}

    iot_client = boto3.client("iot-data", endpoint_url=f"https://{iot_endpoint}")

    # Publish the incoming POST data to the IoT topic
    iot_client.publish(
        topic=topic,
        qos=0,  # QoS 0 = AT_MOST_ONCE
        payload=json.dumps(wrapped_message),
    )

    session = boto3.Session()
    session.get_credentials().get_frozen_credentials()

    REGION = "us-east-1"
    TOPIC = "public/dev-test"
    msg = connect_and_get_first_message(iot_endpoint, REGION, TOPIC, request_id)

    return msg["payload"]

    # return {
    #     "statusCode": 200,
    #     "body": json.dumps(
    #         {
    #             "message": "handler2 called",
    #             # "post_data": post_data,
    #             # "channel_id": channel_id,
    #             # "iot_endpoint": iot_endpoint,
    #             # "wss_url": wss_url,
    #             # "topic": topic,
    #             # "wrapped_message": wrapped_message,
    #             # "TOKEN": os.environ.get("TOKEN"),
    #             # "IOT_ENDPOINT": os.environ.get("IOT_ENDPOINT"),
    #             # "ACCESS_KEY": os.environ.get("ACCESS_KEY"),
    #             # "SECRET_KEY": os.environ.get("SECRET_KEY"),
    #             # "AWS_ACCESS_KEY_ID": creds.access_key,
    #             # "AWS_SECRET_ACCESS_KEY": creds.secret_key,
    #             # "AWS_SESSION_TOKEN": creds.token,
    #             "msg": msg,
    #         }
    #     ),
    # }


class TimeoutException(Exception):
    pass


def sign(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def get_signature_key(key, date_stamp, region, service):
    k_date = sign(("AWS4" + key).encode("utf-8"), date_stamp)
    k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
    k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()


def build_sigv4_ws_url(endpoint, region, access_key, secret_key, session_token=None) -> str:
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
        canonical_querystring += (
            f"&X-Amz-Security-Token={urllib.parse.quote(session_token, safe='')}"
        )

    canonical_headers = f"host:{host}\n"
    signed_headers = "host"
    payload_hash = hashlib.sha256(b"").hexdigest()

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


def connect_and_get_first_message(endpoint, region, topic, request_id, timeout_seconds=30):
    """
    Connects via WebSocket MQTT, subscribes to `topic`,
    returns the first message or raises TimeoutException.
    """

    url = build_sigv4_ws_url(
        endpoint=endpoint,
        region=region,
        access_key=os.environ.get("ACCESS_KEY"),
        secret_key=os.environ.get("SECRET_KEY"),
        session_token=os.environ.get("TOKEN"),
    )

    received_message = {"data": None}
    done_event = threading.Event()

    def on_open(ws) -> None:

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

        # Send MQTT SUBSCRIBE packet (MQTT 3.1.1)
        packet_id = 1
        packet = bytearray()

        # Fixed header
        packet.append(0x82)  # SUBSCRIBE
        variable_header = packet_id.to_bytes(2, "big")

        # Payload
        topic_bytes = topic.encode("utf-8")
        payload = (
            len(topic_bytes).to_bytes(2, "big") + topic_bytes + b"\x00"  # QoS 0
        )

        remaining_length = len(variable_header) + len(payload)
        packet.extend(encode_mqtt_length(remaining_length))
        packet.extend(variable_header)
        packet.extend(payload)

        ws.send(packet, opcode=0x02)  # binary opcode

    def on_message(ws, message) -> None:
        # Raw MQTT frame; parse minimal PUBLISH
        msg = try_parse_mqtt_publish(message)
        if msg is not None and msg.get("requestId") == request_id: # and message["type"] == "request-received":
            # if "payload" in msg:
            #     msg["payload"]["_debug_received_at"] = time.time()
            received_message["data"] = msg
            done_event.set()

    def on_error(ws, err) -> None:
        # Only handle real errors, not close frames
        err_str = str(err)
        if "opcode=8" not in err_str:  # opcode 8 = close frame
            received_message["error"] = err_str
            done_event.set()

    def on_close(ws, close_status_code, close_msg) -> None:
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
    topic_len = int.from_bytes(frame[idx : idx + 2], "big")
    idx += 2
    frame[idx : idx + topic_len].decode()
    idx += topic_len

    # QoS check (ignore packet identifier if QoS1/2)
    # Assume QoS 0 → no packet ID

    payload = frame[idx:]
    try:
        return json.loads(payload.decode("utf-8"))
    except:
        return payload.decode("utf-8")
