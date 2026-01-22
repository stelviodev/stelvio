# SNS Topics

Stelvio supports creating and managing [Amazon SNS (Simple Notification Service)](https://aws.amazon.com/sns/) topics using the `Topic` component. This allows you to build pub/sub messaging patterns where messages are published to a topic and delivered to multiple subscribers.

## Creating a Topic

Create a topic by instantiating the `Topic` component in your `stlv_app.py`:

```python
from stelvio.aws.topic import Topic

@app.run
def run() -> None:
    notifications = Topic("notifications")
```

## FIFO Topics

FIFO topics guarantee strict message ordering and exactly-once delivery:

```python
orders = Topic("orders", fifo=True)
```

When you create a FIFO topic, Stelvio automatically:

- Adds the `.fifo` suffix to the topic name (required by AWS)
- Enables content-based deduplication

!!! info "FIFO Topic Naming"
    AWS requires FIFO topic names to end with `.fifo`. Stelvio handles this automatically when you set `fifo=True`.

!!! warning "FIFO Limitations"
    Lambda functions cannot subscribe directly to FIFO topics. Use `subscribe_queue()` with an SQS queue instead.

## Subscribing Lambda Functions

Subscribe Lambda functions to process messages from your topic:

```python
notifications = Topic("notifications")

# Simple subscription
notifications.subscribe("handler", "functions/notify.process")

# Multiple subscriptions
notifications.subscribe("logger", "functions/logs.record")
notifications.subscribe("auditor", "functions/audit.track")
```

Each subscription creates a separate Lambda function that receives messages published to the topic.

### Lambda Configuration

Customize the Lambda function for your subscription:

```python
# With direct options
notifications.subscribe(
    "processor",
    "functions/notify.process",
    memory=512,
    timeout=60,
)

# With FunctionConfig
from stelvio.aws.function import FunctionConfig

notifications.subscribe(
    "processor",
    FunctionConfig(
        handler="functions/notify.process",
        memory=512,
        timeout=60,
    )
)

# With dictionary
notifications.subscribe(
    "processor",
    {"handler": "functions/notify.process", "memory": 256}
)
```

### Filter Policies

Use filter policies to route specific messages to specific subscribers:

```python
notifications = Topic("notifications")

# Only receive high-priority messages
notifications.subscribe(
    "urgent-handler",
    "functions/urgent.process",
    filter_={"priority": ["high", "critical"]},
)

# Only receive order-related messages
notifications.subscribe(
    "order-handler",
    "functions/orders.process",
    filter_={"type": ["order_created", "order_updated"]},
)
```

Filter policies match against message attributes, not the message body. See [AWS SNS message filtering documentation](https://docs.aws.amazon.com/sns/latest/dg/sns-message-filtering.html) for the full filter policy syntax.

### Processing Messages

Your subscribed Lambda receives SNS events. Here's how to process them:

```python
import json

def process(event, context):
    for record in event.get('Records', []):
        # Get the SNS message
        sns_message = record['Sns']

        # Parse the message body
        body = json.loads(sns_message['Message'])

        # Access message attributes (used for filtering)
        attributes = sns_message.get('MessageAttributes', {})
        message_type = attributes.get('type', {}).get('Value')

        print(f"Processing {message_type}: {body}")

    return {"statusCode": 200}
```

!!! tip "SNS → SQS → Lambda"
    For more control over message processing (retries, batching, dead-letter queues), use `subscribe_queue()` to send messages to an SQS queue, then process them with `queue.subscribe()`. This pattern also works with FIFO topics where direct Lambda subscription isn't supported. See [Working with Queues](queues.md) for details.

## Subscribing SQS Queues

Subscribe SQS queues to receive messages from your topic:

```python
from stelvio.aws.topic import Topic
from stelvio.aws.queue import Queue

notifications = Topic("notifications")
analytics_queue = Queue("analytics")

# Subscribe a Queue component
notifications.subscribe_queue("analytics", analytics_queue)

# Subscribe using a queue ARN
notifications.subscribe_queue(
    "external",
    "arn:aws:sqs:us-east-1:123456789012:external-queue"
)
```

When you subscribe a `Queue` component, Stelvio automatically creates the necessary SQS policy to allow SNS to send messages to the queue.

### Queue Subscription Options

```python
notifications.subscribe_queue(
    "analytics",
    analytics_queue,
    filter_={"type": ["analytics_event"]},  # Filter policy
    raw_message_delivery=True,               # Send raw message without SNS envelope
)
```

| Option                 | Default | Description                                        |
|------------------------|---------|----------------------------------------------------|
| `filter_`              | `None`  | SNS filter policy for message filtering            |
| `raw_message_delivery` | `False` | Send raw message body without SNS metadata wrapper |

!!! info "Raw Message Delivery"
    When `raw_message_delivery=True`, SNS sends just the message body to the queue without wrapping it in SNS metadata (MessageId, Timestamp, etc.). This is useful when your queue processor expects a specific message format. Note that raw message delivery only works with SQS subscriptions, not Lambda subscriptions.

### FIFO Topics with SQS

FIFO topics can deliver to both FIFO and standard SQS queues, but message ordering is only preserved with FIFO queues:

```python
orders = Topic("orders", fifo=True)
orders_queue = Queue("orders-processing", fifo=True)

orders.subscribe_queue("processor", orders_queue)
```

## Publishing Messages

Use the [linking mechanism](linking.md) to publish messages to a topic from Lambda functions.

First, link the topic to your function in `stlv_app.py`:

```python
from stelvio.aws.topic import Topic
from stelvio.aws.function import Function

notifications = Topic("notifications")

# Link the topic so this function can publish to it
publisher = Function(
    "publisher",
    handler="functions/publish.handler",
    links=[notifications],
)
```

Then in your handler, use the linked topic ARN to publish:

```python
import boto3
import json
from stlv_resources import Resources

def handler(event, context):
    sns = boto3.client('sns')

    # Access the linked topic ARN
    topic_arn = Resources.notifications.topic_arn

    # Publish a message
    response = sns.publish(
        TopicArn=topic_arn,
        Message=json.dumps({
            "user_id": "12345",
            "action": "signup_completed",
        }),
        MessageAttributes={
            "type": {
                "DataType": "String",
                "StringValue": "user_event"
            },
            "priority": {
                "DataType": "String",
                "StringValue": "high"
            }
        }
    )

    return {"statusCode": 200, "body": "Message published!"}
```

### Publishing to FIFO Topics

FIFO topics require a `MessageGroupId`:

```python
import boto3
import json
from stlv_resources import Resources

def handler(event, context):
    sns = boto3.client('sns')

    topic_arn = Resources.orders.topic_arn

    response = sns.publish(
        TopicArn=topic_arn,
        Message=json.dumps({"order_id": "12345", "status": "created"}),
        # Required for FIFO - messages with same group ID are delivered in order
        MessageGroupId="order-12345",
        # Optional if content-based deduplication is enabled (default in Stelvio)
        # MessageDeduplicationId="unique-id",
    )

    return {"statusCode": 200, "body": "Order event published!"}
```

!!! info "FIFO Message Parameters"
    - **MessageGroupId** (required): Messages with the same group ID are delivered in order. Use different group IDs for messages that can be processed in parallel.
    - **MessageDeduplicationId** (optional): When content-based deduplication is enabled (default in Stelvio), SNS uses a hash of the message body.

### Link Properties

When you link a topic to a Lambda function, these properties are available:

| Property     | Description    |
|--------------|----------------|
| `topic_arn`  | The topic ARN  |
| `topic_name` | The topic name |

### Link Permissions

Linked Lambda functions receive this SNS permission:

- `sns:Publish` - Publish messages to the topic

## Fanout Pattern

A common pattern is to use SNS topics to fan out messages to multiple queues:

```python
from stelvio.aws.topic import Topic
from stelvio.aws.queue import Queue

# Create topic and queues
orders = Topic("orders")
email_queue = Queue("order-emails")
analytics_queue = Queue("order-analytics")
inventory_queue = Queue("inventory-updates")

# Fan out to multiple queues
orders.subscribe_queue("email", email_queue)
orders.subscribe_queue("analytics", analytics_queue)
orders.subscribe_queue("inventory", inventory_queue)

# Each queue has its own processor
email_queue.subscribe("sender", "functions/email.send_order_confirmation")
analytics_queue.subscribe("tracker", "functions/analytics.track_order")
inventory_queue.subscribe("updater", "functions/inventory.update_stock")
```

With this pattern, a single order event triggers three independent processors. Each queue can scale separately, fail independently, and be updated without affecting the others.

## Customization

The `Topic` component supports the `customize` parameter to override underlying Pulumi resource properties. For an overview of how customization works, see the [Customization guide](customization.md).

### Resource Keys

| Resource Key | Pulumi Args Type                                                                      | Description   |
|--------------|---------------------------------------------------------------------------------------|---------------|
| `topic`      | [TopicArgs](https://www.pulumi.com/registry/packages/aws/api-docs/sns/topic/#inputs)  | The SNS topic |

### Example

```python
topic = Topic(
    "my-topic",
    customize={
        "topic": {
            "kms_master_key_id": "alias/my-key",
        }
    }
)
```

## Next Steps

Now that you understand SNS topics, you might want to explore:

- [Working with Queues](queues.md) - SQS queues for reliable message processing
- [Working with Lambda Functions](lambda.md) - Learn more about Lambda configuration
- [Linking](linking.md) - Understand how Stelvio automates IAM permissions
