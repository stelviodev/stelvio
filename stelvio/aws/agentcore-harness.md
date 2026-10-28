
```python
from stelvio import export_output
from stelvio.app import StelvioApp
from stelvio.aws.dynamo_db import DynamoTable, FieldType
from stelvio.aws.function import Function
from stelvio.aws.agentcore import (
    AgentCoreGateway,
    AgentCoreHarness,
    GatewayTarget,
)

app = StelvioApp("shop")


@app.run
def run():
    orders = DynamoTable(
        "orders",
        fields={"order_id": FieldType.STRING},
        partition_key="order_id",
    )
    lookup = Function(
        "lookup-order",
        handler="tools/orders.handler",
        links=[orders],
    )
    order_tools = AgentCoreGateway(
        "order-tools",
        targets=[
            GatewayTarget(
                function=lookup,
                name="get_order",
                description="Look up an order's shipping status and tracking number.",
                input_schema={
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            ),
        ],
    )
    support = AgentCoreHarness(
        "support",
        model="amazon.nova-lite-v1:0",
        system_prompt="Help customers track orders. Always look up the facts.",
        tools=[order_tools],
        memory=False,
        max_iterations=8,
        timeout_seconds=60,
    )
    export_output("orders_table", orders.resources.table.name)
    export_output("support_harness_arn", support.arn)
```


```python
import boto3
from stlv_resources import Resources

client = boto3.client("bedrock-agentcore")


def invoke_support(user_message: str, session_id: str):
    return client.invoke_harness(
        harnessArn=Resources.support.harness_arn,
        runtimeSessionId=session_id,
        messages=[
            {
                "role": "user",
                "content": [{"text": user_message}],
            }
        ],
    )


# The caller consumes response events, handles errors, and delivers the answer to the UI.
```