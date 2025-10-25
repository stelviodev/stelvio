import json
import time
import uuid
from datetime import datetime
import boto3
from stlv_resources import Resources

def handler(event, context):
    """Handle GET requests by sending a message to DynamoDB table."""
    incoming_request = event.get("requestContext", {}).get("http", {})
    
    # Create DynamoDB client
    dynamodb = boto3.client('dynamodb')
    
    # Generate message data
    message_id = str(uuid.uuid4())
    timestamp = datetime.utcnow().isoformat()
    message_content = f"GET request from {incoming_request.get('method', 'UNKNOWN')} {incoming_request.get('path', '/')}"
    
    try:
        # Put item in DynamoDB table (this will trigger the stream)
        dynamodb.put_item(
            TableName=Resources.messages.table_name,
            Item={
                'id': {'S': message_id},
                'timestamp': {'S': timestamp},
                'message': {'S': message_content},
                'sender': {'S': 'api-gateway-get'}
            }
        )

    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Failed to send message",
                "details": str(e)
            }),
        }
    
    time.sleep(10)

    # Get the most recent items in the dynamo table with 'sender': {'S': 'api-gateway-local'}
    try:
        # First, let's see ALL items in the table for debugging
        all_response = dynamodb.scan(
            TableName=Resources.messages.table_name,
            Limit=50
        )
        all_items = all_response.get("Items", [])
        
        # Filter in Python since DynamoDB filtering seems to have issues
        items = [item for item in all_items if item.get('sender', {}).get('S') == 'api-gateway-local']
        
        # Sort by timestamp to get most recent (optional)
        items.sort(key=lambda x: x.get('timestamp', {}).get('S', ''), reverse=True)
        items = items[:1]  # Take only the first (most recent)
        
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Failed to scan recent messages",
                "details": str(e)
            }),
        }


    if items:
        # items[0] is now the most recent item matching "api-gateway-local"
        most_recent_message = items[0]["message"]["S"]
        most_recent_id = items[0]["id"]["S"]
        most_recent_timestamp = items[0]["timestamp"]["S"]
        
        print(f"Found most recent message from api-gateway-local: {most_recent_message} (ID: {most_recent_id})")
        
        return {
            "statusCode": 200,
            "body": json.dumps({
                # "message": "Message sent to real-time processing system!",
                # "messageId": message_id,
                # "timestamp": timestamp,
                # "content": message_content,
                # "mostRecentLocalMessage": {
                #     "message": most_recent_message,
                #     "id": most_recent_id,
                #     "timestamp": most_recent_timestamp
                # }
                "mostRecentLocalMessage":  most_recent_message,
            }),
        }
    else:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "No recent messages found from api-gateway-local.",
                "messageId": message_id,
                "timestamp": timestamp,
                "content": message_content,
                "mostRecentLocalMessage": None
            }),
        }
