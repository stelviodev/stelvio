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

!!! warning "Dicts vs. callables"
    The precedence and merge rules below apply when the customize value for a
    resource key is a **dict**. A **callable** value behaves differently — see
    [Using Callables for Dynamic Customization](#using-callables-for-dynamic-customization).

When you provide dict-based customizations, Stelvio applies them in this order (highest to lowest precedence):

1. **Per-instance customize** - Customizations passed directly to a component instance
2. **Explicit values** - Properties explicitly set on the component (not None)
3. **Global customize** - Customizations from `StelvioAppConfig` (acts as defaults)
4. **Stelvio defaults** - Built-in Stelvio default values

This means:
- Explicit values you set always take precedence over global defaults, *unless* the global customize for that key is a callable (see below)
- Global customize only applies if you don't set an explicit value
- Per-instance customize overrides everything
- Stelvio's sensible defaults remain in place for properties you don't customize

A **global callable** works differently: whatever it returns is used, except `None` values — a `None` means "no opinion", so the existing default or explicit value is kept. This lets a global callable overwrite, extend, or transform defaults, and even override explicit values if it doesn't check for `None`. See [Using Callables for Dynamic Customization](#using-callables-for-dynamic-customization) for the full picture.

!!! note "Shallow Merge (dicts only)"
    This merge behavior applies only when the customize value is a **dict**. Callables aren't merged at all — whatever they return is used as-is (subject to the `None` handling above for global callables).

    The merge is shallow at each property level. If you customize a nested object, 
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

Explicit values take precedence over global defaults:

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

Explicit constructor arguments always override global `customize` defaults, so you don't need to reach for `customize` just to override a global default—only when you need to set a property that isn't exposed as a constructor argument.

!!! note "Constructor arguments don't always match Pulumi property names"
    Stelvio constructor arguments don't always map 1:1 to the Pulumi property
    name you'd use inside `customize`. For example, Stelvio's `memory`
    constructor argument maps to the underlying Pulumi `memory_size` property.
    Check each component's customization guide (linked in the
    [Quick Reference](#quick-reference) table) for the exact property names.

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
    
    # Explicit value overrides the global default: 1024 MB instead of 512
    fn2 = Function("fast-fn", handler="functions/handler.main", memory=1024)
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

When both global and per-instance customizations are provided, the precedence is (for dict-based customize values — see [Using Callables](#using-callables-for-dynamic-customization) for callables):

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
        memory=1024,  # Explicit value takes precedence over the global default
    )
    
    # Per-instance customize overrides everything: memory_size = 2048
    fn3 = Function(
        "fn3",
        handler="handlers.handler",
        customize={"function": {"memory_size": 2048}}  # Highest precedence
    )
```

## Using Callables for Dynamic Customization

For any resource key you can pass a **callable** instead of a dictionary. The callable receives the resource's properties as a dictionary and returns the properties to use — handy when a value has to be computed rather than hard-coded.

A callable **fully replaces** the properties with whatever it returns, so spread the incoming `props` to keep the values you don't want to change:

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

- **Per-instance `customize`** — the callable receives the fully resolved properties, with Stelvio defaults, global customize, and explicit values already applied. Whatever it returns is used as-is.
- **Global `customize`** — the callable receives Stelvio's defaults with the *computed* properties on top, where `None` marks a value the user did **not** set explicitly. The non-`None` values it returns are merged on top of Stelvio's defaults. Because the callable sees the explicit values, it decides how to treat them — so it can **overwrite**, **extend**, or **transform** the defaults.

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

!!! note "Nested Customization"
    Some Stelvio components create sub-components rather than Pulumi resources directly. For these, the customization structure mirrors what you'd use when instantiating the sub-component on its own. Component pages mark these keys as **Nested**.
