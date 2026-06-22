# Customizing Pulumi Resource Properties

Stelvio provides high-level abstractions for AWS resources, exposing the most commonly used configuration options through component constructors. However, in some cases you might need fine-grained control of certain aspects of the underlying Pulumi resources that Stelvio creates.

The `customize` parameter allows you to override or extend default Pulumi resource properties without modifying Stelvio's source code.

## When to Use Customization

Use the `customize` parameter when you need to:

- Set Pulumi properties not exposed by Stelvio's API (e.g., `force_destroy` on S3 buckets)
- Override default values that Stelvio sets internally
- Add tags, encryption settings, or other resource-specific configurations
- Configure advanced features like VPC settings or custom IAM policies

## Basic Usage

Pass a `customize` dictionary or callable to any Stelvio component. The dictionary keys correspond to the underlying resources that the component creates:

```python
from stelvio.aws.s3 import Bucket

@app.run
def run() -> None:
    bucket = Bucket(
        "example-bucket",
        customize={
            "bucket": {"force_destroy": True}
        }
    )
```

In this example, `"bucket"` refers to the S3 bucket resource created by the `Bucket` component, and `force_destroy` is a Pulumi property that allows the bucket to be deleted even when it contains objects.

You can also use a callable to dynamically customize based on computed properties:

```python
@app.run
def run() -> None:
    bucket = Bucket(
        "example-bucket",
        customize={
            "bucket": lambda props: {
                **props,
                "force_destroy": True,
                "versioning": {"enabled": True}
            }
        }
    )
```

## Understanding Resource Keys

Each Stelvio component creates one or more underlying Pulumi resources. The `customize` dictionary keys match the resource names defined in the component's resources dataclass.

### S3 Bucket

See [S3 Bucket customization](../components/aws/s3.md#customization) for resource keys and examples.

### Lambda Function

See [Lambda Function customization](../components/aws/lambda.md#customization) for resource keys and examples.

### DynamoDB Table

See [DynamoDB Table customization](../components/aws/dynamo-db.md#customization) for resource keys and examples.

### SQS Queue

See [SQS Queue customization](../components/aws/queues.md#customization) for resource keys and examples.

### SNS Topic

See [SNS Topic customization](../components/aws/topics.md#customization) for resource keys and examples.

### Cron (Scheduled Lambda)

See [Cron customization](../components/aws/cron.md#customization) for resource keys and examples.

### Email (SES)

See [Email customization](../components/aws/email.md#customization) for resource keys and examples.

### Lambda Layer

See [Lambda Layer customization](../components/aws/lambda.md#layer) for resource keys and examples.

### CloudFront Distribution

See [CloudFront Distribution customization](../components/aws/cloudfront-router.md#cloudfrontdistribution) for resource keys and examples.

### Router (CloudFront with Routes)

See [Router customization](../components/aws/cloudfront-router.md#customization) for resource keys and examples.

### S3 Static Website

See [S3 Static Website customization](../components/aws/s3.md#s3staticwebsite) for resource keys and examples.

### Advanced: Subscription Customization

Subscription components (DynamoDB streams, SQS, SNS, S3 events) that create Lambda functions include a nested `function` key. This key accepts the same customization options as `FunctionCustomizationDict`, allowing you to customize the subscription's Lambda function.

| Subscription Type       | Resource Keys                                          |
|-------------------------|--------------------------------------------------------|
| `DynamoSubscription`    | `function` (nested), `event_source_mapping`            |
| `QueueSubscription`     | `function` (nested), `event_source_mapping`            |
| `TopicSubscription`     | `function` (nested), `permission`, `topic_subscription`|
| `BucketNotifySubscription` | `function` (nested), `permission`, `queue_policy`, `topic_policy` |

Example with DynamoDB stream subscription:

```python
from stelvio.aws.dynamo_db import DynamoTable

table = DynamoTable(
    "orders",
    fields={"id": "string"},
    partition_key="id",
    stream="new-and-old-images",
)

# Subscribe with function customization
table.subscribe(
    "functions/stream_handler.process",
    customize={
        "function": {
            "function": {"memory_size": 512, "timeout": 60}
        },
        "event_source_mapping": {
            "batch_size": 100,
            "starting_position": "LATEST",
        }
    }
)
```

## How Customization Works

When you provide customizations, Stelvio applies them in this order (highest to lowest precedence):

1. **Per-instance customize** - Customizations passed directly to a component instance
2. **Explicit values** - Properties explicitly set on the component (not None)
3. **Global customize** - Customizations from `StelvioAppConfig` (acts as defaults)
4. **Stelvio defaults** - Built-in Stelvio default values

This means:
- Explicit values you set always take precedence over global defaults
- Global customize only applies if you don't set an explicit value
- Per-instance customize overrides everything
- Stelvio's sensible defaults remain in place for properties you don't customize

!!! note "Shallow Merge"
    The merge is shallow at each property level. If you customize a nested object, 
    your entire object replaces the default, rather than being deep-merged.
    
    For example, if defaults have `{"encryption": {"enabled": true, "kms_key": "key-1"}}` and you provide 
    `{"encryption": {"enabled": false}}`, the result is `{"encryption": {"enabled": false}}`—the `kms_key` is lost.

### Common Pitfalls

#### Nested Object Replacement

When customizing nested objects, the **entire nested object is replaced**, not merged:

```python
# ❌ This replaces entire encryption config - kms_key is lost!
bucket = Bucket(
    "my-bucket",
    customize={"bucket": {"encryption": {"enabled": True}}}
)
# Result: encryption = {"enabled": True} (kms_key removed)

# ✅ To keep existing encryption settings, include them:
bucket = Bucket(
    "my-bucket",
    customize={
        "bucket": {
            "encryption": {
                "enabled": True,
                "kms_key": "arn:aws:kms:...",  # Preserved
            }
        }
    }
)
```

#### Explicit Values Override Global Defaults

With the new behavior, explicit values take precedence over global defaults:

```python
@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        customize={
            Function: {"function": {"memory_size": 512}}
        }
    )

@app.run
def run() -> None:
    # Uses global default: memory_size = 512
    fn1 = Function("fn1", handler="handlers.handler")
    
    # ✅ Explicit value overrides global default: memory_size = 1024
    fn2 = Function(
        "fn2",
        handler="handlers.handler",
        memory_size=1024  # Explicit value takes precedence
    )
```

This is the key difference from the old behavior: you no longer need to use `customize` to override global defaults—explicit constructor arguments work naturally.

## Global Customization

Apply default customizations to all instances of a component type using the `customize` option in `StelvioAppConfig`. Global customizations act as **defaults**—explicit values in component constructors override them:

```python
from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig
from stelvio.aws.s3 import Bucket
from stelvio.aws.function import Function

app = StelvioApp("my-project")

@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        customize={
            Bucket: {
                "bucket": {"force_destroy": True}
            },
            Function: {
                "function": {
                    "memory_size": 512,
                    "tracing_config": {"mode": "Active"}
                }
            }
        }
    )

@app.run
def run() -> None:
    # Both buckets inherit force_destroy=True (global default)
    bucket1 = Bucket("bucket-one")
    bucket2 = Bucket("bucket-two")
    
    # All functions get 512 MB memory and X-Ray tracing (global defaults)
    fn1 = Function("my-fn", handler="functions/handler.main")
    
    # Explicit value overrides global default: 1024 MB instead of 512
    fn2 = Function("fast-fn", handler="functions/handler.main", memory_size=1024)
```

The global `customize` dictionary uses **component types** as keys (e.g., `Bucket`, `Function`) and the same resource customization dictionaries as values.

### Global Customize vs. Explicit Values

Global customize is useful for environment-wide defaults, but explicit values always take precedence:

```python
@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        customize={
            Function: {"function": {"timeout": 30}}
        }
    )

@app.run
def run() -> None:
    # Uses global default: timeout = 30
    fn1 = Function("quick-task", handler="handler.main")
    
    # Explicit value overrides: timeout = 300
    fn2 = Function("slow-task", handler="handler.main", timeout=300)
```

### Combining Global and Per-Instance Customization

When both global and per-instance customizations are provided, the precedence is:

1. **Per-instance** `customize` parameter (highest)
2. **Explicit component constructor values**
3. **Global** `customize` from `StelvioAppConfig` (acts as defaults)
4. **Stelvio defaults** (lowest)

```python
@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        customize={
            Function: {"function": {"memory_size": 512}}
        }
    )

@app.run
def run() -> None:
    # Uses global default: memory_size = 512
    fn1 = Function("fn1", handler="handlers.handler")
    
    # Explicit value overrides global default: memory_size = 1024
    fn2 = Function(
        "fn2",
        handler="handlers.handler",
        memory_size=1024  # Explicit value takes precedence over global default
    )
    
    # Per-instance customize overrides everything: memory_size = 2048
    fn3 = Function(
        "fn3",
        handler="handlers.handler",
        customize={"function": {"memory_size": 2048}}  # Highest precedence
    )
```

## Environment-Specific Customization

Combine customization with environment-based configuration for environment-specific settings:

```python
@app.config
def configuration(env: str) -> StelvioAppConfig:
    if env == "dev":
        return StelvioAppConfig(
            customize={
                Bucket: {"bucket": {"force_destroy": True}},
            }
        )
    else:
        # Production: keep default safe behavior
        return StelvioAppConfig()
```

## Finding Available Properties

To discover which properties you can customize for each resource, refer to the Pulumi AWS provider documentation:

- [S3 Bucket](https://www.pulumi.com/registry/packages/aws/api-docs/s3/bucket/)
- [Lambda Function](https://www.pulumi.com/registry/packages/aws/api-docs/lambda/function/)
- [DynamoDB Table](https://www.pulumi.com/registry/packages/aws/api-docs/dynamodb/table/)
- [SQS Queue](https://www.pulumi.com/registry/packages/aws/api-docs/sqs/queue/)
- [SNS Topic](https://www.pulumi.com/registry/packages/aws/api-docs/sns/topic/)
- [API Gateway REST API](https://www.pulumi.com/registry/packages/aws/api-docs/apigateway/restapi/)

!!! tip "IDE Support"
    If you're using an IDE with Python type checking, the customization dictionaries are fully typed. Your IDE can provide autocompletion and validation for available properties.

## Quick Reference

| Component | Resource Keys | Guide |
|-----------|---------------|-------|
| `Bucket` | `bucket`, `public_access_block`, `bucket_policy`, `bucket_notification`, `subscriptions` (nested), `function`\*, `queue`\*, `topic`\* | [S3](../components/aws/s3.md#customization) |
| `Function` | `function`, `role`, `policy`, `function_url` | [Lambda](../components/aws/lambda.md#customization) |
| `Queue` | `queue` | [Queues](../components/aws/queues.md#customization) |
| `Topic` | `topic` | [Topics](../components/aws/topics.md#customization) |
| `DynamoTable` | `table` | [DynamoDB](../components/aws/dynamo-db.md#customization) |
| `Cron` | `rule`, `target`, `function` (nested) | [Cron](../components/aws/cron.md#customization) |
| `Email` | `identity`, `configuration_set`, `verification`, `event_destinations` | [Email](../components/aws/email.md#customization) |
| `Layer` | `layer_version` | [Lambda](../components/aws/lambda.md#layer) |
| `Api` | `rest_api`, `deployment`, `stage`, `custom_domain`, `base_path_mapping` | [API Gateway](../components/aws/api-gateway.md#customization) |
| `CloudFrontDistribution` | `distribution`, `cache_policy`, `origin_access_control`, `acm_validated_domain` (nested), `record`, `bucket_policy` | [CloudFront](../components/aws/cloudfront-router.md#cloudfrontdistribution) |
| `Router` | `distribution`, `origin_access_controls`, `access_policies`, `cloudfront_functions`, `acm_validated_domain` (nested), `record` | [CloudFront Router](../components/aws/cloudfront-router.md#customization) |
| `S3StaticWebsite` | `bucket` (nested), `files`, `cloudfront_distribution` (nested) | [S3](../components/aws/s3.md#s3staticwebsite) |


!!! note "Nested Customization"
    Some Stelvio components create sub-components rather than Pulumi resources directly. For these, the customization structure mirrors what you'd use when instantiating the sub-component on its own. These cases are marked **(nested)** in the table above.

!!! note "Notification Config Blocks"
    Keys marked with **\*** (`function`, `queue`, `topic` in Bucket) are notification configuration blocks within the `bucket_notification` resource, not standalone Pulumi resources. They customize the notification settings for Lambda, SQS, and SNS targets respectively.