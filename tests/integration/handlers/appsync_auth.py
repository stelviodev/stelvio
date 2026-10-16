"""AppSync authorizer for integration scenarios with allowed and denied tokens."""


def main(event, context):
    return {"isAuthorized": event.get("authorizationToken") == "Bearer allow"}
