#!/usr/bin/env python3
"""
Test script to verify the Lambda-to-IoT integration.
This script:
1. Starts a WebSocket listener on a test channel
2. Sends a POST request to the API Gateway
3. Verifies the message is received via IoT WebSocket
"""
import argparse
import json
import multiprocessing
import requests
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


def listener_process(endpoint, region, identity_pool_id, channel_id, duration, result_queue):
    """Listen for messages on a specific channel."""
    client_id = f"public-test-listener-{uuid.uuid4().hex[:6]}"
    topic = f"public/{channel_id}"
    
    print(f"[Listener] Starting with ID: {client_id}")
    print(f"[Listener] Subscribing to topic: {topic}")
    
    # Get credentials
    creds = get_cognito_credentials(region, identity_pool_id)
    
    # Create credentials provider
    credentials_provider = auth.AwsCredentialsProvider.new_static(
        access_key_id=creds["access_key_id"],
        secret_access_key=creds["secret_access_key"],
        session_token=creds["session_token"],
    )
    
    # Track received messages
    messages_received = []
    
    def on_message(topic, payload, **kwargs):
        msg = payload.decode("utf-8")
        print(f"[Listener] ✅ Received message: {msg}")
        messages_received.append(msg)
        result_queue.put(msg)
    
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
        print(f"[Listener] Connecting...")
        mqtt_connection.connect().result()
        print(f"[Listener] Connected ✓")
        
        # Subscribe
        print(f"[Listener] Subscribing to {topic}...")
        subscribe_future, _ = mqtt_connection.subscribe(
            topic=topic,
            qos=mqtt.QoS.AT_MOST_ONCE,
            callback=on_message,
        )
        subscribe_future.result()
        print(f"[Listener] Subscribed ✓")
        
        # Wait for messages
        print(f"[Listener] Listening for {duration} seconds...")
        time.sleep(duration)
        
        # Disconnect
        print(f"[Listener] Disconnecting... (received {len(messages_received)} messages)")
        mqtt_connection.disconnect().result()
        
    except Exception as e:
        print(f"[Listener] Error: {e}")
        result_queue.put(f"ERROR: {e}")


def send_post_request(api_url, channel_id, data):
    """Send a POST request to the API Gateway."""
    url = f"{api_url}/tunnel/{channel_id}"
    print(f"[Sender] Sending POST to: {url}")
    print(f"[Sender] Data: {data}")
    
    try:
        response = requests.post(url, json=data)
        print(f"[Sender] Response status: {response.status_code}")
        print(f"[Sender] Response body: {response.text}")
        return response
    except Exception as e:
        print(f"[Sender] Error: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Test Lambda-to-IoT integration")
    parser.add_argument("--endpoint", default="a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--identity-pool-id", default="us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931")
    parser.add_argument("--api-url", default="https://r1g9pcls4l.execute-api.us-east-1.amazonaws.com/v1")
    parser.add_argument("--channel-id", default="test-123")
    args = parser.parse_args()
    
    print("=" * 70)
    print("Lambda-to-IoT Integration Test")
    print("=" * 70)
    print(f"IoT Endpoint: {args.endpoint}")
    print(f"Region: {args.region}")
    print(f"API URL: {args.api_url}")
    print(f"Channel ID: {args.channel_id}")
    print("=" * 70)
    print()
    
    # Create a queue to receive messages from the listener process
    result_queue = multiprocessing.Queue()
    
    # Start listener process
    print("Starting listener...")
    listener = multiprocessing.Process(
        target=listener_process,
        args=(args.endpoint, args.region, args.identity_pool_id, args.channel_id, 15, result_queue)
    )
    listener.start()
    
    # Wait for listener to connect and subscribe
    print()
    print("Waiting for listener to connect and subscribe...")
    time.sleep(5)
    
    # Send POST request
    print()
    print("Sending POST request...")
    test_data = {
        "message": "Hello from Lambda!",
        "timestamp": time.time(),
        "test": True
    }
    response = send_post_request(args.api_url, args.channel_id, test_data)
    
    # Wait for message to be received
    print()
    print("Waiting for message to be received via IoT...")
    time.sleep(3)
    
    # Check if we received any messages
    print()
    print("=" * 70)
    messages = []
    while not result_queue.empty():
        messages.append(result_queue.get())
    
    if messages:
        print("✅ SUCCESS! Received messages:")
        for msg in messages:
            print(f"  - {msg}")
        print()
        print("The Lambda function successfully published to IoT!")
    else:
        print("❌ FAILED! No messages received.")
        print()
        print("Check:")
        print("1. Lambda function logs in CloudWatch")
        print("2. IoT Core permissions")
        print("3. API Gateway response for errors")
    
    print("=" * 70)
    
    # Clean up
    listener.terminate()
    listener.join(timeout=2)
    if listener.is_alive():
        listener.kill()
    
    sys.exit(0 if messages else 1)


if __name__ == "__main__":
    main()
