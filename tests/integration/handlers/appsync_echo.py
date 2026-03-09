"""AppSync Lambda data source handler — echoes arguments back as the result."""


def main(event, context):
    return event.get("arguments", {})
