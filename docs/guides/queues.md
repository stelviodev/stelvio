# Working with SQS Queues in Stelvio

Stelvio supports creating and managing [Amazon SQS (Simple Queue Service)](https://aws.amazon.com/sqs/) queues using the `Queue` component. This allows you to build decoupled, event-driven architectures with reliable message delivery.

## Creating a Queue

Create a queue by instantiating the `Queue` component in your `stlv_app.py`:

```python
from stelvio.aws.queue import Queue
from stelvio.aws.function import Function

@app.run
def run() -> None:
    # Create a standard queue
    orders_queue = Queue("orders")

    # Link it to a function
    order_processor = Function(
        "process-orders",
        handler="functions/orders.handler",
        links=[orders_queue],
    )
```

## Queue Configuration

Configure your queue with custom settings:

```python
from stelvio.aws.queue import Queue, QueueConfig

# Using keyword arguments
orders_queue = Queue(
    "orders",
    delay=5,                  # Delay delivery by 5 seconds
    visibility_timeout=60,    # Message hidden for 60 seconds after read
)

# Or using QueueConfig
orders_queue = Queue(
    "orders",
    config=QueueConfig(
        delay=5,
        visibility_timeout=60,
    )
)
```

### Configuration Options

| Option               | Default | Description                                              |
|----------------------|---------|-----------------------------------------------------------|
| `fifo`               | `False` | Enable FIFO (First-In-First-Out) queue ordering           |
| `delay`              | `0`     | Default delay (in seconds) before messages become visible |
| `visibility_timeout` | `30`    | Time (in seconds) a message is hidden after being read    |
| `dlq`                | `None`  | Dead-letter queue configuration                           |

## FIFO Queues

FIFO queues guarantee exactly-once processing and preserve message order:

```python
orders_queue = Queue("orders", fifo=True)
```

When you create a FIFO queue, Stelvio automatically:

- Adds the `.fifo` suffix to the queue name ([required by AWS](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-fifo-queue-message-identifiers.html))
- Enables content-based deduplication

!!! info "FIFO Queue Naming"
    AWS requires FIFO queue names to end with `.fifo`. Stelvio handles this automatically when you set `fifo=True`.

!!! warning "FIFO Throughput"
    FIFO queues have lower [throughput](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/quotas-messages.html) than standard queues (300 messages/second without batching, 3,000 with high-throughput mode). Use standard queues when message order isn't critical.

## Dead-Letter Queues

Configure a dead-letter queue (DLQ) to capture messages that fail processing:

```python
from stelvio.aws.queue import Queue, DlqConfig

# Simple DLQ configuration (default 3 retries)
orders_queue = Queue("orders", dlq="orders-dlq")

# Custom retry count
orders_queue = Queue(
    "orders",
    dlq=DlqConfig(queue="orders-dlq", retry=5)
)

# Or using dictionary syntax
orders_queue = Queue(
    "orders",
    dlq={"queue": "orders-dlq", "retry": 5}
)
```

!!! tip "DLQ Best Practices"
    - Always configure a DLQ for production queues to capture failed messages
    - Set up alerts on your DLQ to detect processing failures
    - Choose retry counts based on your use case (typically 3-5 retries)

## Queue Subscriptions

Subscribe Lambda functions to process messages from your queue:

```python
orders_queue = Queue("orders")

# Simple subscription
orders_queue.subscribe("processor", "functions/orders.process")

# Multiple subscriptions
orders_queue.subscribe("processor", "functions/orders.process")
orders_queue.subscribe("analytics", "functions/analytics.track_order")
```

### Lambda Configuration

Customize the Lambda function for your subscription:

```python
# With direct options
orders_queue.subscribe(
    "processor",
    "functions/orders.process",
    memory=512,
    timeout=60,
)

# With FunctionConfig
from stelvio.aws.function import FunctionConfig

orders_queue.subscribe(
    "processor",
    FunctionConfig(
        handler="functions/orders.process",
        memory=512,
        timeout=60,
    )
)

# With dictionary
orders_queue.subscribe(
    "processor",
    {"handler": "functions/orders.process", "memory": 256}
)
```

### Batch Size

Control how many messages Lambda receives per invocation:

```python
orders_queue.subscribe(
    "batch-processor",
    "functions/orders.process",
    batch_size=5,  # Process 5 messages at a time (default: 10)
)
```

!!! tip "Choosing Batch Size"
    - Smaller batches (1-5): Lower latency, faster processing of individual messages
    - Larger batches (10+): Higher throughput, more efficient for high-volume queues
    - Consider your Lambda timeout when choosing batch size

### Subscription Permissions

Stelvio automatically configures the necessary IAM permissions for queue subscriptions:

- **EventSourceMapping**: Connects the SQS queue to your Lambda function
- **SQS IAM permissions**: Grants read access (`sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes`)

## Sending Messages

Use the [linking mechanism](linking.md) to send messages to your queue from Lambda functions:

```python
import boto3
import json
from stlv_resources import Resources

def handler(event, context):
    sqs = boto3.client('sqs')

    # Access the linked queue URL
    queue_url = Resources.orders.queue_url

    # Send a message
    response = sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps({
            "order_id": "12345",
            "customer": "john@example.com",
            "items": [{"sku": "WIDGET-001", "qty": 2}]
        })
    )

    return {"statusCode": 200, "body": "Message sent!"}
```

### Link Properties

When you link a queue to a Lambda function, these properties are available:

| Property     | Description                        |
|--------------|------------------------------------|
| `queue_url`  | The queue URL for sending messages |
| `queue_arn`  | The queue ARN                      |
| `queue_name` | The queue name                     |

### Link Permissions

Linked Lambda functions receive these SQS permissions:

- `sqs:SendMessage` - Send messages to the queue
- `sqs:ReceiveMessage` - Read messages from the queue
- `sqs:DeleteMessage` - Delete processed messages
- `sqs:GetQueueAttributes` - Read queue metadata
- `sqs:GetQueueUrl` - Retrieve the queue URL

## Processing Messages

Your Lambda function receives SQS events with batched messages:

```python
import json

def process(event, context):
    """Process SQS messages."""
    for record in event.get('Records', []):
        # Parse the message body
        body = json.loads(record['body'])
        
        order_id = body.get('order_id')
        customer = body.get('customer')
        
        print(f"Processing order {order_id} for {customer}")
        
        # Process the order...
        
    # Return success - SQS will delete processed messages
    return {"statusCode": 200}
```

!!! important "Error Handling"
    - If your Lambda raises an exception, SQS will retry the message after the visibility timeout
    - Successfully processed messages are automatically deleted
    - Failed messages eventually move to the DLQ (if configured)

## Linking vs Subscriptions

| Use Case                   | Approach                                                    |
|----------------------------|-------------------------------------------------------------|
| **Process queue messages** | Use `queue.subscribe()` - creates Lambda triggered by queue |
| **Send messages to queue** | Use `links=[queue]` - grants permissions to send messages   |
| **Both read and write**    | Use both subscription AND link to another queue             |

```python
# Example: Process orders, send to fulfillment queue
orders_queue = Queue("orders")
fulfillment_queue = Queue("fulfillment")

# Subscribe to process incoming orders
orders_queue.subscribe(
    "process-orders",
    "functions/orders.process",
    links=[fulfillment_queue],  # Grant permission to send to fulfillment
)
```

## Next Steps

Now that you understand SQS queues, you might want to explore:

- [Working with Lambda Functions](lambda.md) - Learn more about Lambda configuration
- [Working with DynamoDB](dynamo-db.md) - Store processed message data
- [Linking](linking.md) - Understand how Stelvio automates IAM permissions
