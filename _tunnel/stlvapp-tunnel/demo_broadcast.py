#!/usr/bin/env python3
"""
Demo script showing AWS IoT broadcast functionality with multiple clients.
"""
import argparse
import multiprocessing
import os
import sys
import time
import uuid

import boto3
from awscrt import auth, mqtt
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


def listener_client(client_num, endpoint, region, identity_pool_id, topic, duration):
    """Run a listener client."""
    client_id = f"public-listener-{client_num}-{uuid.uuid4().hex[:6]}"
    
    print(f"[Client {client_num}] Starting with ID: {client_id}")
    
    # Get credentials
    creds = get_cognito_credentials(region, identity_pool_id)
    
    # Create credentials provider
    credentials_provider = auth.AwsCredentialsProvider.new_static(
        access_key_id=creds["access_key_id"],
        secret_access_key=creds["secret_access_key"],
        session_token=creds["session_token"],
    )
    
    # Message counter
    messages_received = []
    
    def on_message(topic, payload, **kwargs):
        msg = payload.decode("utf-8")
        messages_received.append(msg)
        print(f"[Client {client_num}] ✅ Received: {msg}")
    
    # Build connection
    mqtt_connection = mqtt_connection_builder.websockets_with_default_aws_signing(
        endpoint=endpoint,
        region=region,
        credentials_provider=credentials_provider,
        on_connection_interrupted=lambda conn, err, **kw: None,
        on_connection_resumed=lambda conn, rc, sp, **kw: None,
        client_id=client_id,
        clean_session=True,
        keep_alive_secs=30,
    )
    
    try:
        # Connect
        print(f"[Client {client_num}] Connecting...")
        mqtt_connection.connect().result()
        print(f"[Client {client_num}] Connected ✓")
        
        # Subscribe
        print(f"[Client {client_num}] Subscribing to {topic}...")
        subscribe_future, _ = mqtt_connection.subscribe(
            topic=topic,
            qos=mqtt.QoS.AT_MOST_ONCE,
            callback=on_message,
        )
        subscribe_future.result()
        print(f"[Client {client_num}] Subscribed ✓")
        
        # Wait for messages
        print(f"[Client {client_num}] Listening for {duration} seconds...")
        time.sleep(duration)
        
        # Disconnect
        print(f"[Client {client_num}] Disconnecting... (received {len(messages_received)} messages)")
        mqtt_connection.disconnect().result()
        
        return len(messages_received)
    except Exception as e:
        print(f"[Client {client_num}] Error: {e}")
        return 0


def broadcaster_client(endpoint, region, identity_pool_id, topic, message, delay):
    """Send a broadcast message after a delay."""
    time.sleep(delay)
    
    client_id = f"public-broadcaster-{uuid.uuid4().hex[:6]}"
    print(f"[Broadcaster] Starting with ID: {client_id}")
    
    # Get credentials
    creds = get_cognito_credentials(region, identity_pool_id)
    
    # Create credentials provider
    credentials_provider = auth.AwsCredentialsProvider.new_static(
        access_key_id=creds["access_key_id"],
        secret_access_key=creds["secret_access_key"],
        session_token=creds["session_token"],
    )
    
    # Build connection
    mqtt_connection = mqtt_connection_builder.websockets_with_default_aws_signing(
        endpoint=endpoint,
        region=region,
        credentials_provider=credentials_provider,
        on_connection_interrupted=lambda conn, err, **kw: None,
        on_connection_resumed=lambda conn, rc, sp, **kw: None,
        client_id=client_id,
        clean_session=True,
        keep_alive_secs=30,
    )
    
    try:
        # Connect
        print(f"[Broadcaster] Connecting...")
        mqtt_connection.connect().result()
        print(f"[Broadcaster] Connected ✓")
        
        # Publish
        print(f"[Broadcaster] Publishing to {topic}: {message}")
        publish_future, _ = mqtt_connection.publish(
            topic=topic,
            payload=message.encode("utf-8"),
            qos=mqtt.QoS.AT_MOST_ONCE,
        )
        publish_future.result()
        print(f"[Broadcaster] Published ✓")
        
        # Disconnect
        time.sleep(1)
        mqtt_connection.disconnect().result()
        print(f"[Broadcaster] Done")
    except Exception as e:
        print(f"[Broadcaster] Error: {e}")


def main():
    parser = argparse.ArgumentParser(description="AWS IoT broadcast demo with multiple clients")
    parser.add_argument("--endpoint", default="a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--identity-pool-id", default="us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931")
    parser.add_argument("--topic", default="public/broadcast")
    parser.add_argument("--num-listeners", type=int, default=3, help="Number of listener clients")
    parser.add_argument("--message", default="🎉 Hello from the broadcaster! This is a test message.")
    args = parser.parse_args()
    
    print("=" * 70)
    print("AWS IoT Core Broadcast Demo")
    print("=" * 70)
    print(f"Endpoint: {args.endpoint}")
    print(f"Region: {args.region}")
    print(f"Topic: {args.topic}")
    print(f"Listeners: {args.num_listeners}")
    print("=" * 70)
    print()
    
    # Start listener processes
    print(f"Starting {args.num_listeners} listener clients...")
    listener_processes = []
    for i in range(1, args.num_listeners + 1):
        p = multiprocessing.Process(
            target=listener_client,
            args=(i, args.endpoint, args.region, args.identity_pool_id, args.topic, 15)
        )
        p.start()
        listener_processes.append(p)
        time.sleep(0.5)  # Stagger starts slightly
    
    print()
    print("Waiting for listeners to connect and subscribe...")
    time.sleep(5)
    
    print()
    print("Starting broadcaster...")
    broadcaster_process = multiprocessing.Process(
        target=broadcaster_client,
        args=(args.endpoint, args.region, args.identity_pool_id, args.topic, args.message, 0)
    )
    broadcaster_process.start()
    
    # Wait for broadcaster to finish
    broadcaster_process.join()
    
    print()
    print("Waiting for listeners to receive message...")
    time.sleep(3)
    
    print()
    print("Cleaning up...")
    for p in listener_processes:
        p.terminate()
        p.join(timeout=2)
        if p.is_alive():
            p.kill()
    
    print()
    print("=" * 70)
    print("Demo complete!")
    print("If you see '✅ Received' messages above, broadcasting is working!")
    print("=" * 70)


if __name__ == "__main__":
    main()
