# Customizing Pulumi Resource Properties

Stelvio provides high-level abstractions for AWS resources, exposing the most commonly used configuration options through component constructors. However, your architecture might require fine-grained control over the underlying Pulumi resources that Stelvio creates.

The `customize` parameter allows you to override or extend default Pulumi resource properties without modifying Stelvio's source code.

## When to Use Customization

Use the `customize` parameter when you need to:

- Set Pulumi properties not exposed by Stelvio's API (e.g., `force_destroy` on S3 buckets)
- Override default values that Stelvio sets internally
- Add tags, encryption settings, or other resource-specific configurations
- Configure advanced features like VPC settings or custom IAM policies

## Basic Usage

Pass a `customize` dictionary to any Stelvio component. The dictionary keys correspond to the underlying resources that the component creates:

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

## Understanding Resource Keys

Each Stelvio component creates one or more underlying Pulumi resources. The `customize` dictionary keys match the resource names defined in the component's resources dataclass.

### Example: S3 Bucket

The `Bucket` component creates these resources:

| Resource Key           | Pulumi Resource Type                    | Description                                   |
|------------------------|-----------------------------------------|-----------------------------------------------|
| `bucket`               | `pulumi_aws.s3.Bucket`                  | The S3 bucket itself                          |
| `public_access_block`  | `pulumi_aws.s3.BucketPublicAccessBlock` | Public access block settings                  |
| `bucket_policy`        | `pulumi_aws.s3.BucketPolicy`            | Bucket policy (when `access="public"`)        |

You can customize any of these:

```python
bucket = Bucket(
    "my-bucket",
    customize={
        "bucket": {
            "force_destroy": True,
            "tags": {"Environment": "dev"},
        },
        "public_access_block": {
            "block_public_acls": True,
        },
    }
)
```

### Example: Lambda Function

The `Function` component creates these resources:

| Resource Key    | Pulumi Resource Type             | Description                        |
|-----------------|----------------------------------|------------------------------------|
| `function`      | `pulumi_aws.lambda_.Function`    | The Lambda function                |
| `role`          | `pulumi_aws.iam.Role`            | IAM execution role                 |
| `policy`        | `pulumi_aws.iam.Policy`          | IAM policy attached to the role    |
| `function_url`  | `pulumi_aws.lambda_.FunctionUrl` | Function URL (when configured)   |

```python
from stelvio.aws.function import Function

fn = Function(
    "my-function",
    handler="functions/handler.main",
    customize={
        "function": {
            "reserved_concurrent_executions": 10,
            "tracing_config": {"mode": "Active"},
        }
    }
)
```

### Example: DynamoDB Table

The `DynamoTable` component creates:

| Resource Key | Pulumi Resource Type          | Description           |
|--------------|-------------------------------|-----------------------|
| `table`      | `pulumi_aws.dynamodb.Table`   | The DynamoDB table    |

```python
from stelvio.aws.dynamo_db import DynamoTable

table = DynamoTable(
    name="orders",
    fields={"id": "string"},
    partition_key="id",
    customize={
        "table": {
            "tags": {"Project": "my-app"},
            "point_in_time_recovery": {"enabled": True},
        }
    }
)
```

### Example: SQS Queue

The `Queue` component creates:

| Resource Key | Pulumi Resource Type    | Description       |
|--------------|-------------------------|-------------------|
| `queue`      | `pulumi_aws.sqs.Queue`  | The SQS queue     |

```python
from stelvio.aws.queue import Queue

queue = Queue(
    "my-queue",
    customize={
        "queue": {
            "tags": {"Team": "backend"},
            "kms_master_key_id": "alias/my-key",
        }
    }
)
```

### Example: SNS Topic

The `Topic` component creates:

| Resource Key | Pulumi Resource Type    | Description       |
|--------------|-------------------------|-------------------|
| `topic`      | `pulumi_aws.sns.Topic`  | The SNS topic     |

```python
from stelvio.aws.topic import Topic

topic = Topic(
    "my-topic",
    customize={
        "topic": {
            "tags": {"Service": "notifications"},
            "kms_master_key_id": "alias/my-key",
        }
    }
)
```

### Example: Cron (Scheduled Lambda)

The `Cron` component creates these resources:

| Resource Key | Pulumi Resource Type               | Description                          |
|--------------|------------------------------------|--------------------------------------|
| `rule`       | `pulumi_aws.cloudwatch.EventRule`  | The EventBridge rule with schedule   |
| `target`     | `pulumi_aws.cloudwatch.EventTarget`| The target linking rule to Lambda    |
| `function`   | (nested `FunctionCustomizationDict`) | The Lambda function (see Function) |

```python
from stelvio.aws.cron import Cron

cron = Cron(
    "my-cron",
    "rate(1 hour)",
    "functions/cleanup.handler",
    customize={
        "rule": {
            "tags": {"Schedule": "hourly"},
        },
        "target": {
            "retry_policy": {"maximum_event_age_in_seconds": 3600},
        }
    }
)
```

### Example: Email (SES)

The `Email` component creates these resources:

| Resource Key          | Pulumi Resource Type                              | Description                           |
|-----------------------|---------------------------------------------------|---------------------------------------|
| `identity`            | `pulumi_aws.sesv2.EmailIdentity`                  | The SES email identity                |
| `configuration_set`   | `pulumi_aws.sesv2.ConfigurationSet`               | SES configuration set                 |
| `verification`        | `pulumi_aws.ses.DomainIdentityVerification`       | Domain verification (for domains)     |
| `event_destinations`  | `pulumi_aws.sesv2.ConfigurationSetEventDestination` | Event destination (when configured) |

```python
from stelvio.aws.email import Email

email = Email(
    "my-email",
    "notifications@example.com",
    customize={
        "identity": {
            "tags": {"Service": "notifications"},
        },
        "configuration_set": {
            "tags": {"Environment": "production"},
        }
    }
)
```

### Example: Lambda Layer

The `Layer` component creates:

| Resource Key    | Pulumi Resource Type                | Description              |
|-----------------|-------------------------------------|--------------------------|
| `layer_version` | `pulumi_aws.lambda_.LayerVersion`   | The Lambda layer version |

```python
from stelvio.aws.layer import Layer

layer = Layer(
    "my-layer",
    dependencies=["requests", "boto3"],
    customize={
        "layer_version": {
            "description": "Shared dependencies layer",
        }
    }
)
```

### Example: CloudFront Distribution

The `CloudFrontDistribution` component creates these resources:

| Resource Key            | Pulumi Resource Type                          | Description                              |
|-------------------------|-----------------------------------------------|------------------------------------------|
| `distribution`          | `pulumi_aws.cloudfront.Distribution`          | The CloudFront distribution              |
| `cache_policy`          | `pulumi_aws.cloudfront.CachePolicy`           | Cache policy for the distribution        |
| `origin_access_control` | `pulumi_aws.cloudfront.OriginAccessControl`   | OAC for secure S3 access                 |
| `dns_record`            | DNS provider Record                           | DNS record (when custom domain set)      |
| `acm`                   | (nested `AcmCustomizationDict`)               | ACM certificate resources                |

```python
from stelvio.aws.cloudfront import CloudFrontDistribution

cdn = CloudFrontDistribution(
    "my-cdn",
    bucket=my_bucket,
    customize={
        "distribution": {
            "price_class": "PriceClass_All",
            "tags": {"CDN": "production"},
        },
        "cache_policy": {
            "comment": "Custom cache policy",
        }
    }
)
```

### Example: Router (CloudFront with Routes)

The `Router` component creates these resources:

| Resource Key            | Pulumi Resource Type                          | Description                              |
|-------------------------|-----------------------------------------------|------------------------------------------|
| `distribution`          | `pulumi_aws.cloudfront.Distribution`          | The CloudFront distribution              |
| `origin_access_controls`| `pulumi_aws.cloudfront.OriginAccessControl`   | OAC for each origin                      |
| `access_policies`       | `pulumi_aws.s3.BucketPolicy`                  | Bucket policies for S3 origins           |
| `cloudfront_functions`  | `pulumi_aws.cloudfront.Function`              | CloudFront functions (e.g., 404 handler) |
| `acm_validated_domain`  | (nested `AcmCustomizationDict`)               | ACM certificate resources                |
| `record`                | DNS provider Record                           | DNS record (when custom domain set)      |

```python
from stelvio.aws.cloudfront.router import Router

router = Router(
    "my-router",
    custom_domain="app.example.com",
    customize={
        "distribution": {
            "price_class": "PriceClass_200",
        },
        "record": {
            "ttl": 300,
        }
    }
)
```

### Example: S3 Static Website

The `S3StaticWebsite` component creates these resources:

| Resource Key   | Pulumi Resource Type                   | Description                                    |
|----------------|----------------------------------------|------------------------------------------------|
| `bucket`       | (nested `S3BucketCustomizationDict`)   | The S3 bucket (see Bucket customization)       |
| `bucket_policy`| `pulumi_aws.s3.BucketPolicy`           | Bucket policy for public access                |
| `cloudfront`   | (nested `CloudFrontCustomizationDict`) | CloudFront distribution (see CloudFront above) |

```python
from stelvio.aws.s3 import S3StaticWebsite

website = S3StaticWebsite(
    "my-website",
    directory="./dist",
    custom_domain="www.example.com",
    customize={
        "bucket": {
            "bucket": {"tags": {"Type": "static-assets"}}
        },
        "cloudfront_distribution": {
            "distribution": {"price_class": "PriceClass_100"}
        }
    }
)
```

### Advanced: Subscription Customization

Subscription components (DynamoDB streams, SQS, SNS, S3 events) that create Lambda functions include a nested `function` key. This key accepts the same customization options as `FunctionCustomizationDict`, allowing you to customize the subscription's Lambda function.

| Subscription Type       | Resource Keys                                          |
|-------------------------|--------------------------------------------------------|
| `DynamoSubscription`    | `function` (nested), `event_source_mapping`            |
| `QueueSubscription`     | `function` (nested), `event_source_mapping`            |
| `TopicSubscription`     | `function` (nested), `permission`, `topic_subscription`|
| `BucketNotifySubscription` | `function` (nested), `permission`, `notification`, `topic_policy` |

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

When you provide customizations, Stelvio merges your values with its default configuration:

1. **Stelvio defaults** are applied first
2. **Your customizations** override or extend those defaults

This means you only need to specify the properties you want to change—Stelvio's sensible defaults remain in place for everything else.

!!! note "Shallow Merge"
    The merge is shallow at each property level. If you customize a nested object (like `tags`), your entire object replaces the default, rather than being deep-merged.

## Global Customization

Apply customizations to all instances of a component type using the `customize` option in `StelvioAppConfig`:

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
                    "tracing_config": {"mode": "Active"}
                }
            }
        }
    )

@app.run
def run() -> None:
    # Both buckets inherit force_destroy=True
    bucket1 = Bucket("bucket-one")
    bucket2 = Bucket("bucket-two")
    
    # All functions have X-Ray tracing enabled
    fn = Function("my-fn", handler="functions/handler.main")
```

The global `customize` dictionary uses **component types** as keys (e.g., `Bucket`, `Function`) and the same resource customization dictionaries as values.

### Combining Global and Per-Instance Customization

When both global and per-instance customizations are provided, they are merged with the following precedence (highest to lowest):

1. **Per-instance** `customize` parameter
2. **Global** `customize` from `StelvioAppConfig`
3. **Stelvio defaults**

```python
@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        customize={
            Bucket: {"bucket": {"force_destroy": True}}
        }
    )

@app.run
def run() -> None:
    # Uses global customization: force_destroy=True
    bucket1 = Bucket("bucket-one")
    
    # Per-instance overrides global: force_destroy=False
    bucket2 = Bucket(
        "bucket-two",
        customize={"bucket": {"force_destroy": False}}
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

| Component | Resource Keys |
|-----------|---------------|
| `Bucket` | `bucket`, `public_access_block`, `bucket_policy` |
| `Function` | `function`, `role`, `policy`, `function_url` |
| `Queue` | `queue` |
| `Topic` | `topic` |
| `DynamoTable` | `table` |
| `Cron` | `rule`, `target`, `function` (nested) |
| `Email` | `identity`, `configuration_set`, `verification`, `event_destinations` |
| `Layer` | `layer_version` |
| `Api` | `rest_api`, `deployment`, `stage` |
| `CloudFrontDistribution` | `distribution`, `cache_policy`, `origin_access_control`, `dns_record`, `acm` (nested) |
| `Router` | `distribution`, `origin_access_controls`, `access_policies`, `cloudfront_functions`, `acm_validated_domain` (nested), `record` |
| `S3StaticWebsite` | `bucket` (nested), `bucket_policy`, `cloudfront_distribution` (nested) |