#!/usr/bin/env python3
"""
Test AWS IoT Core using the official AWS IoT Device SDK with Cognito credentials.
"""
import argparse
import json
import os
import sys
import uuid
from concurrent.futures import Future

import boto3
from awscrt import mqtt
from awsiot import mqtt_connection_builder


def get_cognito_credentials(region: str, identity_pool_id: str):
    """Get temporary AWS credentials from Cognito Identity Pool."""
    ci = boto3.client("cognito-identity", region_name=region)
    identity_id = ci.get_id(IdentityPoolId=identity_pool_id)["IdentityId"]
    creds = ci.get_credentials_for_identity(IdentityId=identity_id)["Credentials"]
    return {
        "access_key_id": creds["AccessKeyId"],
        "secret_access_key": creds["SecretKey"],
        "session_token": creds.get("SessionToken"),
    }


def on_connection_interrupted(connection, error, **kwargs):
    print(f"[connection] interrupted: {error}")


def on_connection_resumed(connection, return_code, session_present, **kwargs):
    print(f"[connection] resumed: rc={return_code} session_present={session_present}")


def on_message_received(topic, payload, **kwargs):
    print(f"[message] topic={topic} payload={payload.decode('utf-8')}")


def main():
    parser = argparse.ArgumentParser(description="AWS IoT public MQTT broadcast via SDK")
    parser.add_argument("--endpoint", default=os.getenv("IOT_ENDPOINT_HOST"), help="AWS IoT ATS endpoint host")
    parser.add_argument("--region", default=os.getenv("AWS_REGION"), help="AWS region")
    parser.add_argument("--identity-pool-id", default=os.getenv("IDENTITY_POOL_ID"), help="Cognito Identity Pool ID")
    parser.add_argument("--client-id", help="MQTT client id (must start with public-); default random")
    parser.add_argument("--topic", default=os.getenv("PUBLIC_TOPIC", "public/broadcast"), help="Topic under public/*")
    parser.add_argument("--message", default="hello from SDK", help="Message to publish")
    args = parser.parse_args()

    if not args.endpoint or not args.region or not args.identity_pool_id:
        print("--endpoint, --region, and --identity-pool-id are required", file=sys.stderr)
        sys.exit(2)

    client_id = args.client_id or f"public-{uuid.uuid4().hex[:8]}"
    if not client_id.startswith("public-"):
        print("client id must start with 'public-' per policy", file=sys.stderr)
        sys.exit(2)

    print(f"[setup] endpoint={args.endpoint} region={args.region} client_id={client_id}")
    
    # Get Cognito credentials
    creds = get_cognito_credentials(args.region, args.identity_pool_id)
    print(f"[creds] access_key={creds['access_key_id'][:5]}...")

    # Build MQTT connection using Cognito credentials
    mqtt_connection = mqtt_connection_builder.websockets_with_default_aws_signing(
        endpoint=args.endpoint,
        region=args.region,
        credentials_provider=None,  # We'll use static credentials
        ca_filepath=None,  # Use default CA certs
        on_connection_interrupted=on_connection_interrupted,
        on_connection_resumed=on_connection_resumed,
        client_id=client_id,
        clean_session=True,
        keep_alive_secs=30,
    )
    
    # Actually, we need to create a proper credentials provider
    # Let's use the static credentials provider from awscrt
    from awscrt import auth
    
    credentials_provider = auth.AwsCredentialsProvider.new_static(
        access_key_id=creds["access_key_id"],
        secret_access_key=creds["secret_access_key"],
        session_token=creds["session_token"],
    )
    
    mqtt_connection = mqtt_connection_builder.websockets_with_default_aws_signing(
        endpoint=args.endpoint,
        region=args.region,
        credentials_provider=credentials_provider,
        ca_filepath=None,
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

    # Subscribe
    print(f"[subscribing] {args.topic}")
    subscribe_future, packet_id = mqtt_connection.subscribe(
        topic=args.topic,
        qos=mqtt.QoS.AT_MOST_ONCE,
        callback=on_message_received,
    )
    subscribe_future.result()
    print(f"[subscribed] {args.topic}")

    # Publish
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
