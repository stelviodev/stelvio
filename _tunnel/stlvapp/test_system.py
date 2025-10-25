#!/usr/bin/env python3
"""
Test script for the real-time messaging system.

This script demonstrates how to interact with the system programmatically.
"""

import json
import requests
import time
import boto3
from datetime import datetime


def test_system(api_url, table_name=None):
    """Test the real-time messaging system."""
    print("🧪 Testing Real-Time Messaging System")
    print("=" * 40)
    
    # Test GET endpoint
    print("1️⃣  Testing GET endpoint...")
    try:
        response = requests.get(api_url)
        print(f"   Status: {response.status_code}")
        print(f"   Response: {response.text}")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print()
    
    # Test POST endpoint
    print("2️⃣  Testing POST endpoint...")
    test_message = {
        "message": f"Test message at {datetime.now().isoformat()}",
        "sender": "test-script"
    }
    
    try:
        response = requests.post(
            f"{api_url}/message",
            headers={"Content-Type": "application/json"},
            data=json.dumps(test_message)
        )
        print(f"   Status: {response.status_code}")
        print(f"   Response: {response.text}")
    except Exception as e:
        print(f"   ❌ Error: {e}")
    
    print()
    
    # If table name provided, query DynamoDB directly
    if table_name:
        print("3️⃣  Checking DynamoDB table...")
        try:
            dynamodb = boto3.client('dynamodb')
            
            # Wait a moment for messages to be processed
            print("   Waiting 3 seconds for stream processing...")
            time.sleep(3)
            
            # Query recent messages
            response = dynamodb.scan(
                TableName=table_name,
                Limit=10
            )
            
            messages = response.get('Items', [])
            print(f"   Found {len(messages)} messages in table:")
            
            for item in messages:
                msg_id = item.get('id', {}).get('S', 'unknown')[:8]
                timestamp = item.get('timestamp', {}).get('S', '')[:19]
                message = item.get('message', {}).get('S', '')
                sender = item.get('sender', {}).get('S', '')
                
                print(f"   - [{timestamp}] {sender}: {message} (ID: {msg_id}...)")
                
        except Exception as e:
            print(f"   ❌ Error querying DynamoDB: {e}")
    
    print()
    print("✅ Test completed!")
    print()
    print("💡 To see real-time messages, run:")
    print(f"   python websocket_client.py {table_name or 'your-table-name'}")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python test_system.py <api_url> [table_name]")
        print("Example: python test_system.py https://abc123.execute-api.us-east-1.amazonaws.com/v1 stlvapp-test-messages")
        sys.exit(1)
    
    api_url = sys.argv[1]
    table_name = sys.argv[2] if len(sys.argv) > 2 else None
    
    test_system(api_url, table_name)