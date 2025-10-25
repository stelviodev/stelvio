import json
import urllib3

INVOKE_URL= "https://btf28gpcwj.execute-api.us-east-1.amazonaws.com/v1" # Function is not linkable!

def handler(event, context):
    incoming_request = event.get("requestContext", {}).get("http", {})

    http = urllib3.PoolManager()

    r = http.request('POST', INVOKE_URL,
                 headers={'Content-Type': 'application/json'},
                 body=json.dumps({
                     "incomingRequest": incoming_request
                 }).encode('utf-8'))
    
    # return {
    #     "statusCode": 200,
    #     "body": "Request sent to server.",
    # }

    if r.status == 200:
        return {
            "statusCode": 200,
            "body": r.data.decode('utf-8'),
        }

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Hello from ApiGateway!",
            "method": incoming_request.get("method"),
            "path": incoming_request.get("path")
        }),
    }


def handler2(event, context): # via Lambda Post
    post_data = json.loads(event.get("body", "{}"))
    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Hello from ApiGateway via POST!",
            "receivedData": post_data
        }),
    }