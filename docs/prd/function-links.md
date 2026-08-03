# PRD: Function component-managed internal links

Status: Draft

Target: `stelvio.aws.function.Function`

Origin: extracted from the `feature/ws-api` prototype (see
[summary.md](summary.md) §5 item A)

## Summary

Give `Function` a private, protocol-neutral way for composed Stelvio components to
register links before Lambda materialization. Component-managed links participate in
the same pipeline as user-configured `config.links`: IAM policy statements,
`STLV_` environment variables, and `stlv_resources` codegen. The user's
`FunctionConfig` stays immutable.

This is a general framework capability. The WebSocket API prototype used it for
`execute-api:ManageConnections`, but the mechanism is not WebSocket-specific and
should land independently of any API Gateway component.

## Problem

Today, links on a `Function` come only from the constructor / `FunctionConfig`. That
works when the user wires components explicitly:

```python
Function("worker", handler="functions/jobs.handle", links=[queue, table])
```

It fails for composed components that own or integrate a `Function` and must inject
permissions or properties themselves:

1. **`FunctionConfig` is immutable after construction.** A parent component cannot
   append to `config.links` without rebuilding the Function or exposing a mutation
   API that breaks the frozen-config contract used elsewhere.
2. **Lazy materialization freezes the link set.** Accessing `function.invoke_arn` or
   `.resources` creates the Lambda, role, policy, env vars, and generated resource
   module in one pass. Any link that arrives after that point cannot be reflected
   in codegen or the aggregate IAM policy.
3. **Composed wiring often depends on the parent's own resources.** A parent may
   only know the exact ARN scope, URL, or stage name after it has created its own
   Pulumi resources. The link must be deferred until that moment, yet still applied
   before the Function materializes.
4. **User self-links can recurse.** If a route Function includes its owning API in
   `links`, resolving that link re-enters the API's `.resources` while the API is
   still materializing. The parent must supply the link itself and reject the
   conflicting user link with a clear error.

Without an internal registration path, every composed component that needs this
pattern invents an ad-hoc workaround (mutating config, attaching extra IAM
policies after the fact, or forbidding reuse of existing Functions). Those
workarounds diverge and cannot share codegen.

## Goals

1. Let any composed component register a `Link` on a `Function` before the Function
   materializes, without mutating `FunctionConfig`.
2. Feed registered links through the existing link pipeline (permissions, env vars,
   `stlv_resources` property mappings, and the local-dev bridge environment).
3. Support deferred registration when the link cannot be built until the parent's
   infrastructure exists (initializer hooks keyed by the parent).
4. Make repeated registration of the same link idempotent (shared handlers / shared
   Functions across parents).
5. Reject conflicts with user-configured links of the same name, and reject
   registration after Function resources exist, with actionable errors.
6. Keep the mechanism private until a second public use case justifies a documented
   API.
7. Remain fully additive: existing `Function` construction, user links, and
   components that create Functions (`Cron`, `RestApi`, `HttpApi`, queues, etc.)
   keep their current behavior.

## Non-goals

- A public `Function.add_link()` / user-facing API for post-construction links.
- Changing the shape of `Link`, `LinkConfig`, `AwsPermission`, or link creators.
- Attaching IAM policies to an already-materialized Function. Late policy
  attachment is a separate concern and does not update codegen or env vars.
- Replacing user `links=` configuration. Internal links are additive and
  subordinate to the conflict rules below.
- WebSocket-, HTTP-, or REST-specific semantics. Parents build their own `Link`
  objects; `Function` only stores and applies them.

## Users and use cases

Primary consumers are **Stelvio component authors**, not application developers.

- A composed API component grants its integrated route Lambdas scoped management
  or invoke-adjacent permissions and injects resolved endpoint URLs into
  `stlv_resources`.
- Two composed components share one existing `Function`. Each registers its own
  internal link before either materializes the Function; both links appear in the
  effective set.
- An authorizer or route Function is created as an implicit child of a parent
  component; the parent registers wiring without exposing a mutable config API.
- An application developer who only uses `links=[...]` sees no API change and no
  behavior change.

## Requirements

### R1. Internal link registry

`Function` MUST maintain a private registry of component-managed links, separate
from `FunctionConfig.links`.

Registration MUST be exposed as a private method (prototype name:
`_register_internal_link(link: Link) -> None`).

### R2. Effective links

All link consumers inside `Function` MUST use an effective link list:

```text
effective_links = config.links + registered internal links
```

This includes at least:

- IAM statement extraction for the Function policy;
- `STLV_` environment variable extraction;
- `stlv_resources` / `LinkPropertiesRegistry` property mappings;
- the local-dev bridge environment used when invoking the handler in process.

User `config.environment` continues to take precedence over link-derived env vars.

### R3. Immutability of user config

Registering an internal link MUST NOT mutate `FunctionConfig` or
`config.links`. User-configured links remain the source of truth for what the
application author declared.

### R4. Idempotency and conflicts

| Situation | Behavior |
|---|---|
| Same `Link` registered again (equal by value, same `name`) | No-op (idempotent). |
| Different `Link` with the same `name` already registered | Raise `ValueError` naming the Function and link. |
| User-configured link (or linkable) with the same `name` | Raise `ValueError` explaining the conflict with a component-managed link. |
| Registration after `Function` resources exist | Raise `RuntimeError` instructing the caller to register before accessing `.resources`. |

Name comparison for user links MUST consider both concrete `Link` instances and
`Linkable` objects that expose `.name`.

### R5. Deferred initializers

`Function` MUST support registering a deferred initializer (prototype name:
`_register_internal_link_initializer(key, initializer)`) that runs at the start of
`_create_resources()`, before effective links are read.

- `key` identifies the registering parent so re-registration replaces rather than
  duplicates work for that parent.
- The initializer MAY create parent infrastructure and then call
  `_register_internal_link`.
- Registering an initializer after Function resources exist MUST raise the same
  class of lifecycle `RuntimeError` as late link registration.
- If the Function has already materialized, parents that still need to attach an
  internal link MUST fail clearly; they MUST NOT silently skip wiring.

### R6. Materialization ordering contract for parents

Parents that rely on internal links MUST ensure every required link (or
initializer) is registered before the Function is materialized. In practice:

1. Register the Function route / integration intent on the parent.
2. Register an initializer on the Function keyed by the parent, **or** register
   the concrete link once parent resources are available and before any
   `invoke_arn` / `.resources` access.
3. Only then read Function resource-backed properties.

Sharing one Function across multiple parents is supported only when all parents
register before the first materialization.

### R7. Privacy and documentation

The mechanism SHOULD remain underscore-private. Public docs and the `Function`
constructor guide MUST NOT document it until a second supported use case warrants
promotion. Component author docs / internals notes MAY describe the contract for
in-tree composed components.

### R8. Additive rollout

Shipping this change MUST NOT alter:

- public `Function` constructor signatures;
- resolution of user-only `links`;
- behavior of existing components that create or link Functions without using the
  new API;
- generated resource module content for Functions that have no internal links.

## Codebase impact

### Direct changes

| Area | Effect |
|---|---|
| `stelvio/aws/function/function.py` | Add registry fields, `_register_internal_link`, `_register_internal_link_initializer`, `_effective_links`; switch `_create_resources` and the local-dev env helper to effective links. |
| Function unit tests | Cover idempotency, user-link conflict, late registration, initializer ordering, codegen/env/IAM inclusion of internal links, and multi-parent shared-Function registration. |

### Enabled consumers

Any composed component that today cannot safely inject links becomes unblocked:

- Future `WebSocketApi` (management URLs + `ManageConnections`).
- Potential future cases: API-owned callback permissions, queue/topic fan-in
  metadata, CloudFront Function URL wiring, or other parent-owned endpoint
  injection.
- Existing components (`Cron`, `RestApi`, `HttpApi`, `Queue` subscriptions, etc.)
  do not need to migrate; they keep creating Functions with ordinary
  `links` / implicit configs.

### Cross-cutting effects

- **Link system:** Internal links are ordinary `Link` values. No change to link
  creators, `LinkableMixin`, or user link overrides.
- **Codegen:** `_extract_links_property_mappings` / `create_stlv_resource_file_content`
  automatically include internal link properties once they appear in the effective
  list. Folder-level `LinkPropertiesRegistry` aggregation continues to merge by
  folder path.
- **IAM:** Aggregate Function policy statements include internal-link permissions
  exactly like user-link permissions.
- **Lazy cycles:** Parents can supply properties that would otherwise require the
  Function to link back to the parent publicly, removing a class of
  Function → parent → Function materialization cycles.
- **Error surface:** New, Function-scoped errors for conflicts and late
  registration. Parent components should translate self-link attempts into
  guidance that points at the component-managed path (e.g. "remove `links=[api]`;
  the API injects management access automatically").

### Out of scope for this PRD's implementation PR

WebSocket (or any other) consumer code. Land the `Function` mechanism with its own
tests first; wire consumers in follow-up PRs.

## Design notes

### Why not mutate `FunctionConfig`?

`FunctionConfig` is treated as a frozen declaration of user intent. Mutating it
from a parent would blur authorship, break equality/idempotency assumptions, and
make conflict detection against "what the user wrote" impossible.

### Why not a public `add_link`?

Application authors already have `links=` at construction time. A public mutation
API invites post-hoc linking after other code may have read `.resources`, and
duplicates the lifecycle hazards this PRD exists to control. Keep the escape hatch
private and parent-driven until reuse patterns are clear.

### Why initializers in addition to direct registration?

Direct registration is enough when the `Link` is fully known up front. Many
parents only know scoped ARNs and URLs after creating their own resources, and
those resources must not be created so early that a later validation failure
leaves orphaned infrastructure—or so late that accessing `function.invoke_arn`
freezes the Function first. An initializer runs at Function materialization time,
giving every registered parent one last chance to attach links in a defined order.

### Prototype reference

The `feature/ws-api` branch implements this contract in
`stelvio/aws/function/function.py` and exercises it from
`WebSocketApi` via `_register_internal_link` /
`_register_internal_link_initializer`. Treat that code as a prototype for this
spec, not as the final consumer.

## Validation

Tests MUST cover at least:

- registering one internal link → permissions, env vars, and codegen properties
  appear;
- registering the same link twice → idempotent;
- registering a conflicting internal link name → `ValueError`;
- conflicting user `links` name → `ValueError`;
- registration after `.resources` → `RuntimeError`;
- initializer runs before effective links are consumed;
- initializer registration after materialization → `RuntimeError`;
- two parents register distinct links on one shared Function before either
  materializes → both present in the effective set;
- Functions with only user links behave identically to today (characterization).

## Rollout

1. Land as an independent PR against `main` with Function-focused tests.
2. Changelog: framework / `Function` internal capability (not a user-facing feature
   note unless promoted later).
3. Follow-up PRs may adopt the API from composed components as those components
   are specified or rewritten.
