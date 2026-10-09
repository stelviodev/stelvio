# Other AWS Resources

When you need an AWS resource Stelvio doesn't support, create it with
[`pulumi_aws`](https://www.pulumi.com/registry/packages/aws/api-docs/) in `@app.run`.
`pulumi_aws` comes installed with Stelvio. These resources deploy with the rest of your
app, and `stlv diff` and `stlv deploy` list them under "Other resources".

To change a resource a component already creates, use [customize](customization.md)
instead.

## Creating a resource

This creates an EventBridge bus:

```python
import pulumi_aws

from stelvio.app import StelvioApp
from stelvio.context import context

app = StelvioApp("my-app")


@app.run
def run() -> None:
    pulumi_aws.cloudwatch.EventBus(context().prefix("orders"))
```

## Names

`context().prefix("orders")` returns `my-app-dev-orders` in the `dev` environment, the
same prefix your components get.

Every resource needs a first argument, Pulumi's name for it. Pulumi builds the AWS name
from it and adds a [random tail](naming.md#the-shape). Many resources also have an
argument for the AWS name itself (`name=` on most, `bucket=` on an S3 bucket). Pass the
prefixed name there and AWS gets it as it is:

```python
# AWS name: my-app-dev-orders-1a2b3c4
pulumi_aws.cloudwatch.EventBus(context().prefix("orders"))

# AWS name: my-app-dev-orders
pulumi_aws.cloudwatch.EventBus("orders", name=context().prefix("orders"))
```

AWS limits how long a name can be, for example 63 characters for an S3 bucket. The whole
AWS name counts: the prefix (`my-app-dev-`), your name, and the random tail's 8 characters
(`-` and 7 random characters) when Pulumi adds one. Keep your part short enough to fit.
Pulumi or AWS rejects a name that is too long, and the error shows the limit.

## Tags

The resource gets `stelvio:app`, `stelvio:env` and your [global tags](tags.md#global-tags).
Pass `tags=` to add more.

## Linking

To let a function use the resource, wrap it in a [`Link`](linking.md#creating-custom-links)
and pass the link to the function. This function may put events on the bus and gets the
bus name:

```python
import pulumi_aws

from stelvio.app import StelvioApp
from stelvio.aws.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.context import context
from stelvio.link import Link

app = StelvioApp("my-app")


@app.run
def run() -> None:
    bus = pulumi_aws.cloudwatch.EventBus(context().prefix("orders"))
    orders_bus = Link(
        name="orders-bus",
        properties={"bus_name": bus.name},
        permissions=[AwsPermission(actions=["events:PutEvents"], resources=[bus.arn])],
    )
    Function("checkout", handler="functions/checkout.handler", links=[orders_bus])
```

Use `bus.name` and `bus.arn`, not names you build yourself. They hold the real values from
AWS, random tail included.

In the handler, the link `orders-bus` becomes `Resources.orders_bus` in the generated
[`stlv_resources.py`](linking.md#generated-resource-access):

```python
import json

import boto3
from stlv_resources import Resources

events = boto3.client("events")


def handler(event, context):
    events.put_events(
        Entries=[
            {
                "EventBusName": Resources.orders_bus.bus_name,
                "Source": "my-app.checkout",
                "DetailType": "OrderPlaced",
                "Detail": json.dumps({"order_id": "123"}),
            }
        ]
    )
```

## Next Steps

- [Linking](linking.md) - Custom links and the generated `stlv_resources.py`
- [Tagging](tags.md) - Auto-tags and global tags
- [Resource Naming](naming.md) - How Stelvio names AWS resources
- [Customization](customization.md) - Change the resources components create
