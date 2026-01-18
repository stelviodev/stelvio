# Scheduled Tasks with Cron

This guide explains how to schedule Lambda function execution using EventBridge Rules. The `Cron` component allows you to run functions on a recurring schedule using rate or cron expressions.

## Creating a scheduled task

The simplest way to create a scheduled task is to provide a name, schedule expression, and handler:

```python
from stelvio.aws.cron import Cron

# Run every hour
Cron("hourly-cleanup", "rate(1 hour)", "tasks/cleanup.handler")

# Run at 2 AM UTC daily
Cron("nightly-report", "cron(0 2 * * ? *)", "tasks/report.handler")
```

## Schedule expressions

Stelvio supports two types of schedule expressions:

### Rate expressions

Use rate expressions for simple recurring schedules:

```python
Cron("every-minute", "rate(1 minute)", "tasks/ping.handler")
Cron("every-5-minutes", "rate(5 minutes)", "tasks/check.handler")
Cron("hourly", "rate(1 hour)", "tasks/sync.handler")
Cron("daily", "rate(1 day)", "tasks/report.handler")
```

Rate expression format: `rate(value unit)`

- **value**: A positive number
- **unit**: `minute`, `minutes`, `hour`, `hours`, `day`, or `days`

!!! note
    Use singular form (`minute`, `hour`, `day`) when value is 1, plural otherwise.

### Cron expressions

Use cron expressions for more complex schedules:

```python
# Every day at 9:30 AM UTC
Cron("morning-report", "cron(30 9 * * ? *)", "tasks/report.handler")

# Every Monday at 8 AM UTC
Cron("weekly-digest", "cron(0 8 ? * MON *)", "tasks/digest.handler")

# First day of each month at midnight
Cron("monthly-cleanup", "cron(0 0 1 * ? *)", "tasks/cleanup.handler")
```

Cron expression format: `cron(minutes hours day-of-month month day-of-week year)`

| Field | Values | Wildcards |
|-------|--------|-----------|
| Minutes | 0-59 | , - * / |
| Hours | 0-23 | , - * / |
| Day-of-month | 1-31 | , - * ? / L W |
| Month | 1-12 or JAN-DEC | , - * / |
| Day-of-week | 1-7 or SUN-SAT | , - * ? L # |
| Year | 1970-2199 | , - * / |

!!! warning
    All cron schedules run in UTC. Plan accordingly for your timezone.

## Function configuration

### Handler with options

Pass function options alongside the handler:

```python
Cron("heavy-task",
    "rate(1 hour)",
    "tasks/process.handler",
    memory=1024,
    timeout=300
)
```

### Using FunctionConfig

For complete control, use `FunctionConfig`:

```python
from stelvio.aws.function import FunctionConfig

config = FunctionConfig(
    handler="tasks/process.handler",
    memory=2048,
    timeout=600,
    environment={"BATCH_SIZE": "1000"}
)

Cron("batch-job", "rate(6 hours)", config)
```

### Using an existing Function

Reuse a Function across multiple cron jobs:

```python
from stelvio.aws.function import Function

processor = Function("data-processor",
    handler="tasks/process.handler",
    memory=2048
)

# Different schedules, same function
Cron("hourly-process", "rate(1 hour)", processor)
Cron("daily-full-process", "rate(1 day)", processor)
```

## Options

### Disabling a schedule

Create a schedule in disabled state:

```python
Cron("maintenance-job",
    "rate(1 hour)",
    "tasks/maintenance.handler",
    enabled=False
)
```

### Custom payload

Pass a custom JSON payload to the Lambda function:

```python
Cron("batch-job",
    "rate(1 hour)",
    "tasks/batch.handler",
    payload={"mode": "incremental", "batch_size": 100}
)

# Full sync on weekends
Cron("weekend-sync",
    "cron(0 0 ? * SAT *)",
    "tasks/batch.handler",
    payload={"mode": "full"}
)
```

The payload is passed as the event to your Lambda handler:

```python
def handler(event, context):
    mode = event.get("mode", "incremental")
    batch_size = event.get("batch_size", 50)
    # Process based on payload
```

## Linking resources

Connect your scheduled function to other resources using links:

```python
from stelvio.aws.cron import Cron
from stelvio.aws.dynamo_db import DynamoTable

orders_table = DynamoTable(
    name="orders",
    fields={"order_id": "string", "status": "string"},
    partition_key="order_id"
)

Cron("process-orders",
    "rate(5 minutes)",
    "tasks/orders.handler",
    links=[orders_table]
)
```

## AWS resources created

For each Cron component, Stelvio creates:

- **EventBridge Rule**: The schedule trigger
- **EventBridge Target**: Links the rule to the Lambda function
- **Lambda Permission**: Allows EventBridge to invoke the function
- **Lambda Function**: If not using an existing Function

## Common patterns

### Cleanup job

```python
Cron("cleanup-old-data",
    "rate(1 day)",
    "tasks/cleanup.handler",
    payload={"retention_days": 30},
    timeout=900
)
```

### Health check

```python
Cron("health-check",
    "rate(5 minutes)",
    "tasks/health.handler",
    timeout=30
)
```

### Report generation

```python
Cron("daily-report",
    "cron(0 6 * * ? *)",  # 6 AM UTC daily
    "tasks/report.handler",
    memory=1024,
    timeout=300,
    payload={"report_type": "daily"}
)
```
