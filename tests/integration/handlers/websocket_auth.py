def authorize(event, context):
    token = (event.get("queryStringParameters") or {}).get("token")
    effect = "Allow" if token == "allow" else "Deny"
    return {
        "principalId": "websocket-user",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": event["methodArn"],
                }
            ],
        },
    }
