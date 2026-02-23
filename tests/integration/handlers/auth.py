def handler(event, context):
    """Simple token authorizer that allows 'Bearer allow' tokens."""
    token = event.get("authorizationToken", "")
    method_arn = event["methodArn"]

    effect = "Allow" if token == "Bearer allow" else "Deny"

    return {
        "principalId": "user",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": method_arn,
                }
            ],
        },
    }
