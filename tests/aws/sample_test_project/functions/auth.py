"""Simple auth handler for testing AppSync Lambda authorization."""


def handler(event, context):
    """Authorize request."""
    return {"isAuthorized": True}
