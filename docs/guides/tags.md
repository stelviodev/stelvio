# Tagging AWS Resources

Stelvio supports two tagging layers:

1. Global app tags from `StelvioAppConfig.tags`
2. Per-component tags via `tags={...}` on components

This lets you set organization-wide tags once, and still override or extend tags for specific resources.

## Global Tags (App-Level)

Set global tags in `@app.config`:

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

Stelvio applies these through AWS provider `default_tags`, together with auto-tags:

- `stelvio:app`
- `stelvio:env`

## Component Tags (Resource-Level)

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

## Precedence Rules

If the same key exists at multiple levels, Stelvio resolves values in this order:

1. Auto tags (`stelvio:app`, `stelvio:env`)
2. Global tags (`StelvioAppConfig.tags`)
3. Component tags (`tags=...` on a component)
4. `customize` overrides (if you explicitly set `tags` in customize)

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
- auto tags are still present

## Composite Component Propagation

For components that create internal components/resources (for example subscription-generated Lambda functions), Stelvio propagates component tags to those generated internals where tagging is supported.

Examples:

- `Queue(..., tags=...)` + `queue.subscribe(...)` -> generated subscription function inherits tags
- `Topic(..., tags=...)` + `topic.subscribe(...)` -> generated subscription function inherits tags
- `Api(..., tags=...)` + string/config handlers -> generated route functions inherit tags

## Non-Taggable Resources

Not every AWS resource type supports tags. Stelvio applies tags where AWS supports them and skips non-taggable resource types.

!!! note
    AWS Lambda `LayerVersion` does not accept `tags` in the current Pulumi AWS provider API. Because of that, Stelvio `Layer` does not expose a `tags` parameter.
