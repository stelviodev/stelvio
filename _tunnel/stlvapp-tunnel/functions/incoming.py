

import json
import boto3
import os


def handler(event, context):
    channel_id = event["pathParameters"]["channel_id"]
    incoming_post_data = json.loads(event.get("body", "{}"))

    # Get IoT endpoint from environment or use default
    iot_endpoint = os.environ.get("IOT_ENDPOINT", "a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com")
    wss_url = f"wss://{iot_endpoint}/mqtt"
    
    # Publish to IoT topic
    topic = f"public/{channel_id}"
    
    try:
        # Create IoT Data client
        iot_client = boto3.client('iot-data', endpoint_url=f"https://{iot_endpoint}")
        
        # Publish the incoming POST data to the IoT topic
        response = iot_client.publish(
            topic=topic,
            qos=0,  # QoS 0 = AT_MOST_ONCE
            payload=json.dumps(incoming_post_data)
        )
        
        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "success": True,
                    "incoming_channel_id": channel_id,
                    "incoming_post_data": {
                        "payload": incoming_post_data
                    },
                    "published_to_topic": topic,
                    "wss_url": wss_url,
                }
            ),
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps(
                {
                    "success": False,
                    "error": str(e),
                    "channel_id": channel_id,
                }
            ),
        }