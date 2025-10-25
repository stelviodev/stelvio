import json
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
        
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "Message sent to real-time processing system!",
                "messageId": message_id,
                "timestamp": timestamp,
                "content": message_content
            }),
        }
        
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": "Failed to send message",
                "details": str(e)
            }),
        }


