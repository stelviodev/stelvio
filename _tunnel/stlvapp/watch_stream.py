#!/usr/bin/env python3
"""
Watch DynamoDB Stream for real-time changes.
"""

import uuid
import boto3
import json
import time
import sys
import threading
from datetime import datetime, timezone


def on_event(event_name, msg_id, db_record):
    """Placeholder for event handling logic."""
    print("Event received:", event_name, msg_id, db_record)

    def reply():
        try:
            dynamodb.put_item(
                TableName=table_name,
                Item={
                    'id': {'S': message_id},
                    'timestamp': {'S': timestamp},
                    'message': {'S': message_content},
                    'sender': {'S': 'api-gateway-local'}
                }
            )
        except Exception as e:
            print(f"❌ Error sending message: {e}")
            # Handle error (e.g., log it, send a notification, etc.)

    dynamodb = boto3.client('dynamodb')
    message_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    message_content = "Sample message content"
    table_name = 'stlvapp-vscode-messages-125cad6'  # Replace with your table name

    # get dynamo item with  msg_id
    try:
        # Build the key from all the keys in the stream record
        key = {}
        for key_name, key_value in db_record['Keys'].items():
            key[key_name] = key_value
        
        response = dynamodb.get_item(
            TableName=table_name,
            Key=key
        )
        item = response.get('Item')
        if item:
            if 'sender' in item:
                if item['sender']['S'] == 'api-gateway-get':
                    reply()
        else:
            print("DynamoDB item not found")
    except Exception as e:
        print(f"❌ Error getting DynamoDB item: {e}")



    # return

    


def process_shard(stream_arn, shard_id, shard_index):
    """Process records from a single shard."""
    client = boto3.client('dynamodbstreams')
    
    try:
        # Get shard iterator
        iterator_response = client.get_shard_iterator(
            StreamArn=stream_arn,
            ShardId=shard_id,
            ShardIteratorType='LATEST'
        )
        
        shard_iterator = iterator_response['ShardIterator']
        
        while True:
            try:
                # Get records
                records_response = client.get_records(ShardIterator=shard_iterator)
                records = records_response.get('Records', [])
                
                # Process any records found
                for record in records:
                    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    event_name = record['eventName']
                    
                    # Extract data based on event type
                    if 'dynamodb' in record:
                        db_record = record['dynamodb']
                        
                        # Get item data
                        if 'NewImage' in db_record:
                            new_image = db_record['NewImage']
                            msg_id = new_image.get('id', {}).get('S', 'unknown')[:8]
                            message = new_image.get('message', {}).get('S', 'no message')
                            sender = new_image.get('sender', {}).get('S', 'unknown')
                            
                            print(f"[{timestamp}] [Shard-{shard_index}] {event_name}: {sender} - {message} (ID: {msg_id}...)")
                        
                        elif 'Keys' in db_record:
                            keys = db_record['Keys']
                            msg_id = keys.get('id', {}).get('S', 'unknown')[:8]
                            print(f"[{timestamp}] [Shard-{shard_index}] {event_name}: Item with ID {msg_id}...")
                            on_event(event_name, msg_id, db_record)
                
                # Get next iterator
                next_iterator = records_response.get('NextShardIterator')
                if next_iterator:
                    shard_iterator = next_iterator
                else:
                    print(f"Shard-{shard_index}: No more records available")
                    break
                    
            except Exception as e:
                print(f"❌ Shard-{shard_index} Error reading records: {e}")
                time.sleep(5)
                continue
            
            # Small delay between polls
            time.sleep(2)
            
    except Exception as e:
        print(f"❌ Shard-{shard_index} Error: {e}")


def watch_stream(stream_arn):
    """Watch a DynamoDB stream for changes."""
    client = boto3.client('dynamodbstreams')
    
    print(f"🔍 Watching DynamoDB Stream...")
    print(f"Stream ARN: {stream_arn}")
    print("Press Ctrl+C to stop\n")
    
    try:
        # Get stream description
        response = client.describe_stream(StreamArn=stream_arn)
        shards = response['StreamDescription']['Shards']
        
        if not shards:
            print("❌ No shards found in stream")
            return
        
        print(f"Found {len(shards)} shards")
        
        # Process all shards using threading
        threads = []
        for i, shard in enumerate(shards):
            shard_id = shard['ShardId']
            print(f"Starting thread for Shard-{i}: {shard_id}")
            
            thread = threading.Thread(
                target=process_shard,
                args=(stream_arn, shard_id, i),
                daemon=True
            )
            thread.start()
            threads.append(thread)
        
        print(f"\n✅ Started {len(threads)} threads to monitor all shards\n")
        
        # Keep main thread alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n👋 Stream watching stopped")
            return
            
    except KeyboardInterrupt:
        print("\n👋 Stream watching stopped")
    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python watch_stream.py <stream_arn>")
        sys.exit(1)
    
    stream_arn = sys.argv[1]
    watch_stream(stream_arn)