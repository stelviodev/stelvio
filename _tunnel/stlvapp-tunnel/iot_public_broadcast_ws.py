#!/usr/bin/env python3
"""
WebSocket-based MQTT client for AWS IoT Core using Cognito Identity Pool (unauth) credentials.
Uses the AWS IoT Device SDK for Python v2 for proper WebSocket authentication.

What it does:
- Gets temporary AWS creds from your Identity Pool (unauthenticated identity)
- Connects to AWS IoT Core using WebSocket with SigV4 authentication
- Subscribes to a topic and publishes messages
- Broadcasts messages to all connected subscribers

Usage:
  python iot_public_broadcast_ws.py \
    --endpoint a1xxxxxxxxxxxx-ats.iot.us-east-1.amazonaws.com \
    --region us-east-1 \
    --identity-pool-id us-east-1:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx \
    --topic public/broadcast \
    --message "hello from websockets"

Env vars (optional): IOT_ENDPOINT_HOST, AWS_REGION, IDENTITY_POOL_ID, PUBLIC_TOPIC

Notes:
- Client IDs must start with 'public-' to satisfy the deployed IAM policy.
- Topics must be under 'public/*'.
- The SDK handles WebSocket connection, SigV4 signing, and MQTT protocol details.
"""

import argparse
import os
import sys
import uuid

import boto3
from awscrt import auth, mqtt
from awsiot import mqtt_connection_builder


def get_cognito_credentials(region: str, identity_pool_id: str):
    """Get temporary AWS credentials from Cognito Identity Pool (unauthenticated)."""
    ci = boto3.client("cognito-identity", region_name=region)
    identity_id = ci.get_id(IdentityPoolId=identity_pool_id)["IdentityId"]
    creds = ci.get_credentials_for_identity(IdentityId=identity_id)["Credentials"]
    
    # Debug: show which role we're using
    try:
        sts = boto3.client(
            "sts",
            region_name=region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretKey"],
            aws_session_token=creds.get("SessionToken"),
        )
        ident = sts.get_caller_identity()
        print(f"[debug] caller_identity: {ident.get('Arn')}")
    except Exception as e:
        print(f"[warn] failed to get caller identity: {e}", file=sys.stderr)
    
    return {
        "access_key_id": creds["AccessKeyId"],
        "secret_access_key": creds["SecretKey"],
        "session_token": creds.get("SessionToken"),
    }


def on_connection_interrupted(connection, error, **kwargs):
    """Callback when connection is interrupted."""
    print(f"[connection] interrupted: {error}")


def on_connection_resumed(connection, return_code, session_present, **kwargs):
    """Callback when connection is resumed."""
    print(f"[connection] resumed: rc={return_code} session_present={session_present}")


def on_message_received(topic, payload, **kwargs):
    """Callback when a message is received."""
    try:
        text = payload.decode("utf-8")
    except Exception:
        text = str(payload)
    print(f"[message] topic={topic} payload={text}")


def main():
    parser = argparse.ArgumentParser(description="AWS IoT public MQTT broadcast via websockets")
    parser.add_argument(
        "--endpoint",
        default=os.getenv("IOT_ENDPOINT_HOST"),
        help="AWS IoT ATS endpoint host (no scheme, no path)"
    )
    parser.add_argument(
        "--region",
        default=os.getenv("AWS_REGION"),
        help="AWS region, e.g. us-east-1"
    )
    parser.add_argument(
        "--identity-pool-id",
        default=os.getenv("IDENTITY_POOL_ID"),
        help="Cognito Identity Pool ID (e.g. us-east-1:uuid)"
    )
    parser.add_argument(
        "--client-id",
        help="MQTT client id (must start with public-); default random"
    )
    parser.add_argument(
        "--topic",
        default=os.getenv("PUBLIC_TOPIC", "public/broadcast"),
        help="Topic under public/*"
    )
    parser.add_argument(
        "--message",
        default="hello from websockets",
        help="Message to publish after connect"
    )
    args = parser.parse_args()

    if not args.endpoint or not args.region or not args.identity_pool_id:
        print(
            "--endpoint, --region, and --identity-pool-id are required (or set env vars)",
            file=sys.stderr
        )
        sys.exit(2)

    client_id = args.client_id or f"public-{uuid.uuid4().hex[:8]}"
    if not client_id.startswith("public-"):
        print("client id must start with 'public-' per policy", file=sys.stderr)
        sys.exit(2)

    print(f"[setup] endpoint={args.endpoint} region={args.region} client_id={client_id}")
    
    # Get Cognito credentials
    creds = get_cognito_credentials(args.region, args.identity_pool_id)
    print(f"[debug] access_key={creds['access_key_id'][:5]}...")

    # Create credentials provider for AWS IoT SDK
    credentials_provider = auth.AwsCredentialsProvider.new_static(
        access_key_id=creds["access_key_id"],
        secret_access_key=creds["secret_access_key"],
        session_token=creds["session_token"],
    )
    
    # Build MQTT connection using WebSocket with SigV4 signing
    mqtt_connection = mqtt_connection_builder.websockets_with_default_aws_signing(
        endpoint=args.endpoint,
        region=args.region,
        credentials_provider=credentials_provider,
        ca_filepath=None,  # Use default CA certs
        on_connection_interrupted=on_connection_interrupted,
        on_connection_resumed=on_connection_resumed,
        client_id=client_id,
        clean_session=True,
        keep_alive_secs=30,
    )

    print("[connecting]")
    connect_future = mqtt_connection.connect()
    connect_future.result()
    print("[connected]")

    # Subscribe to the topic
    print(f"[subscribing] {args.topic}")
    subscribe_future, packet_id = mqtt_connection.subscribe(
        topic=args.topic,
        qos=mqtt.QoS.AT_MOST_ONCE,
        callback=on_message_received,
    )
    subscribe_future.result()
    print(f"[subscribed] {args.topic}")

    # Publish initial message if provided
    if args.message:
        print(f"[publishing] {args.topic}: {args.message}")
        publish_future, packet_id = mqtt_connection.publish(
            topic=args.topic,
            payload=args.message.encode("utf-8"),
            qos=mqtt.QoS.AT_MOST_ONCE,
        )
        publish_future.result()
        print("[published]")

    # Keep connection alive to receive messages
    print("[listening] Press Ctrl+C to exit...")
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[disconnecting]")
        disconnect_future = mqtt_connection.disconnect()
        disconnect_future.result()
        print("[disconnected]")


if __name__ == "__main__":
    main()
