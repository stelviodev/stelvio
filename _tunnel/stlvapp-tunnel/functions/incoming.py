

import json
import boto3
import os
import uuid
import time


def wait_for_response(iot_client, channel_id, request_id, timeout=10):
    """
    Wait for a response message by polling IoT Thing Shadow.
    The tunnel service should update the shadow with the response.
    """
    thing_name = f"{channel_id}"
    start_time = time.time()
    poll_interval = 0.1  # Poll every 100ms
    
    c = 0
    last_shadow_payload = None
    while time.time() - start_time < timeout:

        try:
            response = iot_client.get_thing_shadow(thingName=thing_name)
        except Exception as e:
            return {"message": "Error polling shadow", "error": str(e)}

        try:
            # Get the thing shadow
            response = iot_client.get_thing_shadow(thingName=thing_name)
            shadow_payload = json.loads(response['payload'].read())
            last_shadow_payload = shadow_payload
            
            # Check if shadow contains our response
            if 'state' in shadow_payload and 'reported' in shadow_payload['state']:
                reported = shadow_payload['state']['reported']
                if reported.get('requestId') == request_id and reported.get('type') == 'request-processed':
                    # Found matching response
                    return reported.get('payload', {})
        except iot_client.exceptions.ResourceNotFoundException:
            # Shadow doesn't exist yet, continue polling
            # return {"message": "Shadow not found"}
            c += 1
            if c > 20:
                return {"message": f"Shadow not found after {c} attempts, last payload: {last_shadow_payload}"}
        except Exception as e:
            # Log error but continue polling
            return {"message": "Error polling shadow", "error": str(e)}
        
        time.sleep(poll_interval)
    
    # Timeout reached
    raise TimeoutError(f"No response received within {timeout} seconds")


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

        # Create a thing shadow name based on channel ID
        thing_name = f"tunnel-{channel_id}"  # --- IGNORE --
        


        # const wrappedMessage = JSON.stringify({
		# 			payload: JSON.parse(payload),
		# 			requestId: requestId,
		# 			type: "request-received"
		# 		});

        wrapped_message = {
            "payload": incoming_post_data,
            "type": "request-received",
            "requestId": uuid.uuid4().hex
        }
        
        # Publish the incoming POST data to the IoT topic
        response = iot_client.publish(
            topic=topic,
            qos=0,  # QoS 0 = AT_MOST_ONCE
            payload=json.dumps(wrapped_message)
        )

        # Wait for response from tunnel service
        try:
            response_payload = wait_for_response(iot_client, channel_id, wrapped_message["requestId"], timeout=10)
            
            # Clean up the shadow after receiving response
            thing_name = f"tunnel-{channel_id}"
            try:
                iot_client.delete_thing_shadow(thingName=thing_name)
            except:
                pass  # Ignore cleanup errors
            
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "response": response_payload
                })
            }
        except TimeoutError:
            return {
                "statusCode": 500,
                "body": json.dumps({
                    "response": "Timeout"
                })
            }
    except Exception as e:
        return {
            "statusCode": 500,
            "body": json.dumps({
                "error": str(e),
                "channel_id": channel_id,
            })
        }
