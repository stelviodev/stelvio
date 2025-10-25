import json
import uuid
from datetime import datetime
import boto3
from stlv_resources import Resources


def handler(event, context):
    """
    Process DynamoDB stream events and echo messages back to the table.
    This simulates the AppSync WebSocket API behavior by processing stream events.
    """
    dynamodb = boto3.client('dynamodb')
    
    processed_messages = []
    
    # Process each record from the DynamoDB stream
    for record in event.get('Records', []):
        event_name = record['eventName']  # INSERT, MODIFY, REMOVE
        
        if event_name == 'INSERT':
            # New message inserted - process and echo it back
            new_item = record['dynamodb']['NewImage']
            
            # Extract message data
            original_id = new_item.get('id', {}).get('S', '')
            original_message = new_item.get('message', {}).get('S', '')
            original_sender = new_item.get('sender', {}).get('S', '')
            original_timestamp = new_item.get('timestamp', {}).get('S', '')
            
            # Skip processing if this is already an echo message to avoid infinite loops
            if original_sender.startswith('echo-processor'):
                continue
            
            # Create echo message
            echo_id = str(uuid.uuid4())
            echo_timestamp = datetime.utcnow().isoformat()
            echo_message = f"ECHO: {original_message}"
            echo_sender = "echo-processor"
            
            try:
                # Put echo message back into the table
                dynamodb.put_item(
                    TableName=Resources.messages.table_name,
                    Item={
                        'id': {'S': echo_id},
                        'timestamp': {'S': echo_timestamp},
                        'message': {'S': echo_message},
                        'sender': {'S': echo_sender}
                    }
                )
                
                processed_messages.append({
                    "original_id": original_id,
                    "original_message": original_message,
                    "echo_id": echo_id,
                    "echo_message": echo_message
                })
                
                # Print to CloudWatch logs (simulates WebSocket output)
                print(f"[REAL-TIME ECHO] {echo_timestamp}: {echo_message}")
                
            except Exception as e:
                print(f"Error processing message {original_id}: {str(e)}")
                
        elif event_name == 'MODIFY':
            # Message updated
            old_item = record['dynamodb']['OldImage']
            new_item = record['dynamodb']['NewImage']
            print(f"Message updated: {old_item} -> {new_item}")
            
        elif event_name == 'REMOVE':
            # Message deleted
            old_item = record['dynamodb']['OldImage']
            print(f"Message deleted: {old_item}")
    
    return {
        "statusCode": 200,
        "processed_count": len(processed_messages),
        "processed_messages": processed_messages
    }