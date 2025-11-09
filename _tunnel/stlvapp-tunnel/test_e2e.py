#!/usr/bin/env python3
"""
End-to-end test combining:
1. WebSocket listeners on a channel
2. HTTP POST to Lambda (which publishes to IoT)
3. Verify all listeners receive the message
"""
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


def listener_client(client_num, endpoint, region, identity_pool_id, channel_id, duration):
    """Run a listener client."""
    client_id = f"public-listener-{client_num}-{uuid.uuid4().hex[:6]}"
    topic = f"public/{channel_id}"
    
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


def main():
    endpoint = "a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com"
    region = "us-east-1"
    identity_pool_id = "us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931"
    api_url = "https://r1g9pcls4l.execute-api.us-east-1.amazonaws.com/v1"
    channel_id = "demo-channel"
    num_listeners = 3
    
    print("=" * 70)
    print("End-to-End Lambda-to-IoT Broadcast Test")
    print("=" * 70)
    print(f"Endpoint: {endpoint}")
    print(f"Region: {region}")
    print(f"Channel: {channel_id}")
    print(f"Listeners: {num_listeners}")
    print("=" * 70)
    print()
    
    # Start listener processes
    print(f"Starting {num_listeners} listener clients...")
    listener_processes = []
    for i in range(1, num_listeners + 1):
        p = multiprocessing.Process(
            target=listener_client,
            args=(i, endpoint, region, identity_pool_id, channel_id, 15)
        )
        p.start()
        listener_processes.append(p)
        time.sleep(0.5)  # Stagger starts slightly
    
    print()
    print("Waiting for listeners to connect and subscribe...")
    time.sleep(5)
    
    print()
    print("Sending message via HTTP POST to Lambda...")
    test_data = {
        "message": "🚀 Broadcast from Lambda via HTTP POST!",
        "timestamp": time.time(),
        "broadcast": True
    }
    
    url = f"{api_url}/tunnel/{channel_id}"
    response = requests.post(url, json=test_data)
    print(f"[Sender] Response status: {response.status_code}")
    print(f"[Sender] Response: {response.text}")
    
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
    print("✅ Test complete!")
    print("If you see '✅ Received' messages from all 3 clients above,")
    print("then Lambda successfully published to IoT and all clients received it!")
    print("=" * 70)


if __name__ == "__main__":
    main()
