# Customizing Pulumi Resource Properties

Stelvio provides high-level abstractions for AWS resources, exposing the most commonly used configuration options through component constructors. However, in some cases you might need fine-grained control of certain aspects of the underlying Pulumi resources that Stelvio creates.

The `customize` parameter allows you to override or extend default Pulumi resource properties without modifying Stelvio's source code.

## When to Use Customization

Use the `customize` parameter when you need to:

- Set Pulumi properties not exposed by Stelvio's API (e.g., `force_destroy` on S3 buckets)
- Override default values that Stelvio sets internally
- Add tags, encryption settings, or other resource-specific configurations
- Configure advanced features like VPC settings or custom IAM policies

!!! warning "Customize doesn't change what Stelvio itself reads"
    `customize` only changes what Stelvio sends to AWS. Stelvio's own properties, links and
    IAM roles read the constructor arguments, not `customize`. For these settings, always use
    the argument: `stream=` (DynamoTable), `generate_secret=` (UserPoolClient),
    `allow_unauthenticated=` (IdentityPool), `stage_name=` (RestApi, HttpApi, WebsocketApi),
    `endpoint_type=` (RestApi), `disable_execute_api_endpoint=` (WebsocketApi), `runtime=`
    and `architecture=` (Function). Changing them through `customize` puts AWS and Stelvio
    out of sync, and Stelvio doesn't check for it.

## Basic Usage

Pass a `customize` dictionary to any Stelvio component. The dictionary keys correspond to the underlying resources that the component creates. The value for each key can be either a plain dict of properties or a callable that computes them:

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

You can also use a callable as the value for a resource key to compute properties dynamically. It receives that resource's *computed* properties — not necessarily every Pulumi property, see [Using Callables for Dynamic Customization](#using-callables-for-dynamic-customization) for details — and returns the properties to use. Callables behave differently from dicts: they aren't shallow-merged, and a global callable can even override explicit values — see below.

```python
@app.run
def run() -> None:
    bucket = Bucket(
        "example-bucket",
        customize={
            "bucket": lambda props: {
                **props,
                "force_destroy": True,
            }
        }
    )
```

## Understanding Resource Keys

Each Stelvio component creates one or more underlying Pulumi resources. The `customize` dictionary keys match the resource names defined in the component's resources dataclass.

Each component's page lists its resource keys. The [Quick Reference](#quick-reference) table links to all of them.

### Advanced: Subscription Customization

`subscribe()` on `DynamoTable`, `Queue` and `Topic`, `Topic.subscribe_queue()` and `Bucket.notify_function()` take their own `customize` for the resources the call creates. When the call creates a Lambda function, its `function` key takes the same keys as `Function(customize=...)`.

Keys per component: [DynamoDB](../components/aws/dynamo-db.md#customization), [Queues](../components/aws/queues.md#customization), [Topics](../components/aws/topics.md#customization), [S3](../components/aws/s3.md#notification-function-resource-keys-via-notify_functioncustomize).

Example with DynamoDB stream subscription:

```python
from stelvio.aws.dynamo_db import DynamoTable

table = DynamoTable(
    "orders",
    fields={"id": "string"},
    partition_key="id",
    stream="new-and-old-images",
)

table.subscribe(
    "process-orders",
    "functions/orders.process",
    customize={
        "function": {
            "function": {"reserved_concurrent_executions": 5}
        },
        "event_source_mapping": {
            "maximum_retry_attempts": 3,
            "bisect_batch_on_function_error": True,
        },
    },
)
```

## How Customization Works

Stelvio picks each property's value in this order (highest to lowest precedence):

1. **Per-instance customize**: the `customize` you pass to the component
2. **Global callable**: a callable in `StelvioAppConfig(customize=...)`
3. **Explicit values**: properties you set through constructor arguments (not `None`)
4. **Global dict**: a dict in `StelvioAppConfig(customize=...)`
5. **Stelvio defaults**: built-in default values

The global entry for a resource key is either a dict or a callable, so only one of 2 and 4 applies. A global dict is a default: your constructor arguments beat it. A global callable sees your arguments, and any value it returns that isn't `None` beats them. A key it leaves out or returns as `None` keeps the value it would have had without the callable. See [Using Callables for Dynamic Customization](#using-callables-for-dynamic-customization) for details.

!!! note "Shallow Merge"
    A callable on a component isn't merged: what it returns is exactly what's sent. A global callable's non-`None` values are merged over the other values.

    Dicts are merged shallowly at each property level. If you customize a nested object, 
    your entire object replaces the default, rather than being deep-merged.
    
    For example, if defaults have `{"encryption": {"enabled": true, "kms_key": "key-1"}}` and you provide 
    `{"encryption": {"enabled": false}}`, the result is `{"encryption": {"enabled": false}}`—the `kms_key` is lost.

### Common Pitfalls

#### Nested Object Replacement

This applies to dict-based customize values (see the [Shallow Merge](#how-customization-works) note above — callables aren't merged). When customizing nested objects, the **entire nested object is replaced**, not merged:

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

Explicit values take precedence over a global dict:

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
        memory=1024,  # Explicit value takes precedence
    )
```

Explicit constructor arguments always override a global `customize` dict, so you don't need `customize` just to override a global default. Use it when you need to set a property that isn't exposed as a constructor argument.

!!! note "Constructor arguments don't always match Pulumi property names"
    Stelvio constructor arguments don't always map 1:1 to the Pulumi property
    name you'd use inside `customize`. For example, Stelvio's `memory`
    constructor argument maps to the underlying Pulumi `memory_size` property.
    Check each component's customization guide (linked in the
    [Quick Reference](#quick-reference) table) for the exact property names.

## Global Customization

Apply default customizations to all instances of a component type using the `customize` option in `StelvioAppConfig`. A global dict acts as a **default**: explicit values in component constructors override it. A global callable can override them, see [Using Callables for Dynamic Customization](#using-callables-for-dynamic-customization).

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
    
    # Explicit value overrides the global default: 1024 MB instead of 512
    fn2 = Function("fast-fn", handler="functions/handler.main", memory=1024)
```

The global `customize` dictionary uses **component types** as keys (e.g., `Bucket`, `Function`) and the same resource customization dictionaries as values, except the keys listed in [Keys That Configure Another Component](#keys-that-configure-another-component).

Stelvio checks global `customize` when `@app.config` returns, before any component exists. A key that isn't a component type, a resource key the component doesn't have, or a value that isn't a dict raises an error, also for a type your app never creates.

### Global Customize vs. Explicit Values

A global dict is useful for environment-wide defaults, and explicit values take precedence over it:

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

Per-instance `customize` beats both explicit values and global `customize`. The full order is in [How Customization Works](#how-customization-works).

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
        memory=1024,  # Explicit value takes precedence over the global default
    )
    
    # Per-instance customize overrides everything: memory_size = 2048
    fn3 = Function(
        "fn3",
        handler="handlers.handler",
        customize={"function": {"memory_size": 2048}}  # Highest precedence
    )
```

### Keys That Configure Another Component

Some resource keys configure a component that another component creates, such as the function a `Cron` runs. These keys work only in per-instance `customize`, in the call that creates the component. In global `customize` they raise an error.

```python
from stelvio.aws.cron import Cron

@app.run
def run() -> None:
    # Only this Cron's function gets 1024 MB
    Cron(
        "cleanup",
        "rate(1 day)",
        "functions/cleanup.handler",
        customize={"function": {"function": {"memory_size": 1024}}},
    )
```

| Component | Key | Pass it in |
|-----------|-----|------------|
| `Cron` | `function` | `Cron(...)` |
| `DynamoSubscription` | `function` | `table.subscribe(...)` |
| `QueueSubscription` | `function` | `queue.subscribe(...)` |
| `TopicSubscription` | `function` | `topic.subscribe(...)` |
| `BucketNotifySubscription` | `function` | `bucket.notify_function(...)` |
| `AppSyncDataSource` | `function` | `api.data_source_lambda(...)` |
| `AppSync` | `auth_functions`, `acm_validated_domain` | `AppSync(...)` |
| `UserPool` | `trigger_functions`, `acm_validated_domain` | `UserPool(...)` |
| `RestApi` | `acm_validated_domain` | `RestApi(...)` |
| `CloudFrontDistribution` | `acm_validated_domain` | `CloudFrontDistribution(...)` |
| `Router` | `acm_validated_domain` | `Router(...)` |
| `S3StaticWebsite` | `bucket`, `cloudfront_distribution` | `S3StaticWebsite(...)` |
| `ApiDomain` | `certificate` | `ApiDomain(...)` |

A global `{Function: ...}` applies to every function in the app, including the ones these components create. A nested dict on a component is that function's own per-instance `customize`, so it beats the global entry. A `Function` you create yourself and pass in, for example as a `Cron` handler, keeps its own `customize`: the parent's key doesn't change it.

## Using Callables for Dynamic Customization

For any resource key you can pass a **callable** instead of a dictionary. The callable receives the resource's properties as a dictionary and returns the properties to use — handy when a value has to be computed rather than hard-coded.

On a component, a callable **fully replaces** the properties with whatever it returns, so spread the incoming `props` to keep the values you don't want to change. A global callable works differently, see [What the Callable Receives](#what-the-callable-receives).

```python
Function(
    "my-fn",
    handler="functions/handler.main",
    customize={
        "function": lambda props: {
            **props,
            "description": f"{props['memory_size']} MB function",
        }
    },
)
```

### What the Callable Receives

The properties passed to a callable depend on where you use it:

- **Per-instance `customize`**: the callable receives the fully resolved properties, with Stelvio defaults, global customize, and explicit values already applied. Whatever it returns is used as-is.
- **Global `customize`**: the callable receives Stelvio's defaults with the *computed* properties on top, where `None` marks a value the user did **not** set explicitly. The non-`None` values it returns are merged over Stelvio's defaults and the explicit values. A key it leaves out or returns as `None` keeps the value it would have had without the callable, so you can return only the keys you change or spread `props`. Because the callable sees the explicit values, it decides how to treat them, so it can **overwrite**, **extend**, or **transform** them.

!!! note "Removing a property"
    A global callable can't remove a property: a key it returns as `None` keeps its value. To remove one, use a callable in the component's own `customize` and leave the key out of what it returns.

### Global Callables Act as Defaults

A global callable is the dynamic counterpart of a global dictionary. Check for `None` to honor values the user set explicitly:

```python
def function_defaults(props):
    # Default to 512 MB unless the user set memory explicitly
    memory = props["memory_size"] if props.get("memory_size") is not None else 512
    return {**props, "memory_size": memory}

@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        customize={Function: {"function": function_defaults}},
    )
```

!!! warning "A global callable can override explicit values"
    Unlike a global *dictionary* (where explicit values always win), a global
    *callable* is in full control. Returning `{**props, "memory_size": 512}`
    unconditionally would override even a `Function(..., memory=1024)`. Check
    for `None` whenever you want explicit values to take precedence.

### Adding to a Value Stelvio Sets

A global callable can extend a value instead of replacing it. This adds an environment variable to every function and keeps the ones the function already has:

```python
def add_log_level(props):
    variables = props["environment"]["variables"]
    return {"environment": {"variables": {**variables, "LOG_LEVEL": "info"}}}

@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        customize={Function: {"function": add_log_level}},
    )
```

!!! note "A global dict can't set environment variables"
    Stelvio always sets a function's `environment`: the `STLV_*` variables from links plus
    your `environment=` argument. It counts as an explicit value, so a global dict like
    `{Function: {"function": {"environment": ...}}}` never applies. Use a callable as above,
    or the `environment=` argument.

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
- [API Gateway HTTP API](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/api/)

!!! tip "IDE Support"
    If you're using an IDE with Python type checking, the customization dictionaries are fully typed. Your IDE can provide autocompletion and validation for available properties.

## Quick Reference

| Component | Guide |
|-----------|---------------|
| `Bucket` | [S3](../components/aws/s3.md#customization) |
| `Function` | [Lambda](../components/aws/lambda.md#customization) |
| `Queue` | [Queues](../components/aws/queues.md#customization) |
| `Topic` | [Topics](../components/aws/topics.md#customization) |
| `DynamoTable` | [DynamoDB](../components/aws/dynamo-db.md#customization) |
| `Cron` | [Cron](../components/aws/cron.md#customization) |
| `Email` | [Email](../components/aws/email.md#customization) |
| `Layer` | [Lambda](../components/aws/lambda.md#layer) |
| `RestApi` | [REST API](../components/aws/rest-api.md#customization) |
| `HttpApi` | [HTTP API](../components/aws/http-api.md#customization) |
| `WebsocketApi` | [WebSocket API](../components/aws/websocket-api.md#customization) |
| `ApiDomain` | [HTTP API](../components/aws/http-api.md#customization) |
| `AppSync` | [AppSync](../components/aws/appsync.md#customization) |
| `UserPool`, `IdentityPool` | [Cognito](../components/aws/cognito.md#customization) |
| `CloudFrontDistribution` | [CloudFront](../components/aws/cloudfront-router.md#cloudfrontdistribution) |
| `Router` | [CloudFront Router](../components/aws/cloudfront-router.md#customization) |
| `S3StaticWebsite` | [S3](../components/aws/s3.md#s3staticwebsite) |
| `Vpc` | [VPC](../components/aws/vpc.md#customization) |
| `DocumentDb` | [DocumentDB](../components/aws/document-db.md#customization) |

!!! note "Nested Customization"
    Some Stelvio components create sub-components rather than Pulumi resources directly. For these, the customization structure mirrors what you'd use when instantiating the sub-component on its own. Component pages mark these keys as **Nested**. They work only in per-instance `customize`, see [Keys That Configure Another Component](#keys-that-configure-another-component).
