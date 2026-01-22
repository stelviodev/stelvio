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

See [S3 Bucket customization](s3.md#customization) for resource keys and examples.

### Example: Lambda Function

See [Lambda Function customization](lambda.md#customization) for resource keys and examples.

### Example: DynamoDB Table

See [DynamoDB Table customization](dynamo-db.md#customization) for resource keys and examples.

### Example: SQS Queue

See [SQS Queue customization](queues.md#customization) for resource keys and examples.

### Example: SNS Topic

See [SNS Topic customization](topics.md#customization) for resource keys and examples.

### Example: Cron (Scheduled Lambda)

See [Cron customization](cron.md#customization) for resource keys and examples.

### Example: Email (SES)

See [Email customization](email.md#customization) for resource keys and examples.

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

See [Router customization](cloudfront-router.md#customization) for resource keys and examples.

### Example: S3 Static Website

The `S3StaticWebsite` component creates these resources:

| Resource Key             | Pulumi Resource Type                        | Description                                    |
|--------------------------|---------------------------------------------|------------------------------------------------|
| `bucket`                 | (nested `BucketCustomizationDict`)        | The S3 bucket (see Bucket customization)       |
| `files`                  | `pulumi_aws.s3.BucketObject`                | Uploaded files from the directory              |
| `cloudfront_distribution`| (nested `CloudFrontDistributionCustomizationDict`) | CloudFront distribution (see CloudFront above) |

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
    The merge is shallow at each property level. If you customize a nested object (like `tags`), 
    your entire object replaces the default, rather than being deep-merged.
    
    For example, if defaults have `{"tags": {"a": 1, "b": 2}}` and you provide 
    `{"tags": {"c": 3}}`, the result is `{"tags": {"c": 3}}`—not `{"tags": {"a": 1, "b": 2, "c": 3}}`.

### Common Pitfalls

#### Nested Object Replacement

When customizing nested objects, the **entire nested object is replaced**, not merged:

```python
# Default tags from Stelvio or global customize:
# {"bucket": {"tags": {"Team": "platform", "Cost": "shared"}}}

# ❌ This replaces ALL default tags - Team and Cost are lost!
bucket = Bucket(
    "my-bucket",
    customize={"bucket": {"tags": {"Env": "dev"}}}
)
# Result: tags = {"Env": "dev"}

# ✅ To keep existing tags, include them in your customization:
bucket = Bucket(
    "my-bucket",
    customize={
        "bucket": {
            "tags": {
                "Team": "platform",
                "Cost": "shared",
                "Env": "dev",  # Your addition
            }
        }
    }
)
```

#### Global + Instance Tag Replacement

The same shallow merge applies when combining global and per-instance customization:

```python
@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        customize={
            Bucket: {"bucket": {"tags": {"Team": "platform", "Cost": "shared"}}}
        }
    )

@app.run
def run() -> None:
    # ❌ Per-instance tags completely replace global tags
    bucket = Bucket(
        "my-bucket",
        customize={"bucket": {"tags": {"Env": "dev"}}}
    )
    # Result: tags = {"Env": "dev"} - Team and Cost are gone!
```

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

| Component | Resource Keys | Guide |
|-----------|---------------|-------|
| `Bucket` | `bucket`, `public_access_block`, `bucket_policy` | [S3](s3.md#customization) |
| `Function` | `function`, `role`, `policy`, `function_url` | [Lambda](lambda.md#customization) |
| `Queue` | `queue` | [Queues](queues.md#customization) |
| `Topic` | `topic` | [Topics](topics.md#customization) |
| `DynamoTable` | `table` | [DynamoDB](dynamo-db.md#customization) |
| `Cron` | `rule`, `target`, `function` (nested) | [Cron](cron.md#customization) |
| `Email` | `identity`, `configuration_set`, `verification`, `event_destinations` | [Email](email.md#customization) |
| `Layer` | `layer_version` | — |
| `Api` | `rest_api`, `deployment`, `stage` | [API Gateway](api-gateway.md#customization) |
| `CloudFrontDistribution` | `distribution`, `cache_policy`, `origin_access_control`, `dns_record`, `acm` (nested) | — |
| `Router` | `distribution`, `origin_access_controls`, `access_policies`, `cloudfront_functions`, `acm_validated_domain` (nested), `record` | [CloudFront Router](cloudfront-router.md#customization) |
| `S3StaticWebsite` | `bucket` (nested), `files`, `cloudfront_distribution` (nested) | — |


!!! note "Nested Customization"
    Some Stelvio components create sub-components rather than Pulumi resources directly. For these, the customization structure mirrors what you'd use when instantiating the sub-component on its own. These cases are marked **(nested)** in the table above.