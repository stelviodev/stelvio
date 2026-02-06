"""Shared handler for testing AppSync Lambda data sources."""


def handler(event, context):
    """Handle AppSync request."""
    return {"data": "shared handler response"}
