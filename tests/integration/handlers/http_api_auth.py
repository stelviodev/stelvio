def handler(event, context):
    token = event.get("headers", {}).get("authorization", "")
    return {
        "isAuthorized": token == "Bearer allow",
        "context": {"principalId": "user"},
    }
