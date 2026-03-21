# Tagging AWS Resources

AWS tags are key-value pairs you attach to resources. They show up in the AWS console, Cost Explorer, and billing reports, making it easier to track costs, identify resource owners, enforce policies, and filter resources across accounts.

Stelvio gives you three tagging layers that work together:

1. **Auto-tags** — applied automatically to every resource
2. **Global tags** — set once in `StelvioAppConfig`, applied to all components
3. **Component tags** — set per component for fine-grained control

## Auto-Tags

Stelvio automatically tags every resource with:

| Tag | Value | Example |
|-----|-------|---------|
| `stelvio:app` | Your app name from `StelvioApp("name")` | `my-app` |
| `stelvio:env` | The current deployment environment | `dev`, `staging`, `prod` |

These tags are always present — you don't need to configure anything. They're applied through the AWS provider's `default_tags`, so they appear on every taggable resource Stelvio creates.

This means you can always filter resources in the AWS console or Cost Explorer by app and environment, even if you never set any custom tags.

## Global Tags

Set organization-wide tags in `@app.config` to apply them to all components:

```python
from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig

app = StelvioApp("my-app")


@app.config
def config(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        tags={
            "Team": "platform",
            "CostCenter": "infra",
        }
    )
```

Global tags are applied through AWS provider `default_tags`, alongside auto-tags.

## Component Tags

You can set tags on a specific component:

```python
from stelvio.aws.queue import Queue

orders = Queue(
    "orders",
    tags={
        "Service": "checkout",
        "Owner": "payments-team",
    },
)
```

Component tags are applied to that component's taggable AWS resources.

## Precedence

If the same key exists at multiple levels, higher-specificity wins:

1. Auto-tags (`stelvio:app`, `stelvio:env`) — lowest priority
2. Global tags (`StelvioAppConfig.tags`)
3. Component tags (`tags=...` on a component)
4. `customize` overrides (if you explicitly set `tags` in customize) — highest priority

Example:

```python
from stelvio.aws.queue import Queue
from stelvio.config import StelvioAppConfig


@app.config
def config(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(tags={"Shared": "global", "GlobalOnly": "yes"})


@app.run
def run() -> None:
    Queue("jobs", tags={"Shared": "component", "ComponentOnly": "yes"})
```

Result for `jobs` queue tags:

- `Shared=component` (component overrides global)
- `GlobalOnly=yes`
- `ComponentOnly=yes`
- auto-tags are still present

## Common Patterns

**Cost allocation by team:**

```python
StelvioAppConfig(tags={"CostCenter": "platform"})
```

**Environment-specific tags:**

```python
@app.config
def config(env: str) -> StelvioAppConfig:
    tags = {"Team": "backend"}
    if env == "prod":
        tags["OnCall"] = "backend-oncall"
    return StelvioAppConfig(tags=tags)
```

**Per-service ownership:**

```python
Queue("orders", tags={"Service": "checkout", "Owner": "payments-team"})
Queue("notifications", tags={"Service": "notifications", "Owner": "comms-team"})
```

## Tag Propagation

For components that create internal resources (for example, subscription-generated Lambda functions), Stelvio propagates component tags to those generated internals where tagging is supported.

Examples:

- `Queue(..., tags=...)` + `queue.subscribe(...)` — generated subscription function inherits tags
- `Topic(..., tags=...)` + `topic.subscribe(...)` — generated subscription function inherits tags
- `Api(..., tags=...)` + string/config handlers — generated route functions inherit tags

## Non-Taggable Resources

Not every AWS resource type supports tags. Stelvio applies tags where AWS supports them and skips non-taggable resource types.

!!! note
    AWS Lambda `LayerVersion` does not accept `tags` in the current Pulumi AWS provider API. Because of that, Stelvio `Layer` does not expose a `tags` parameter.
