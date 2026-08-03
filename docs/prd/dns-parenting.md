# PRD: DNS provider parenting

Status: Draft

Target: `stelvio.dns.Dns` protocol and built-in providers
(`Route53Dns`, `CloudflareDns`); callers in ACM and API Gateway domains

Origin: extracted from the `feature/ws-api` prototype (see
[summary.md](summary.md) §5 item B); first surfaced as review-3 parenting gap and
review-4 legacy-provider compatibility

## Summary

Extend Stelvio's DNS abstraction so record creation can accept Pulumi
`ResourceOptions` (notably `parent`) when the provider supports them, while
preserving the legacy call contract for application-supplied DNS adapters that
do not.

Call sites that create DNS records on behalf of a Stelvio component—especially
`AcmValidatedDomain` validation records and API Gateway custom-domain alias
records—MUST parent those records under the owning component. Built-in Route 53
and Cloudflare providers MUST honor `opts`. Custom providers that only implement
the historical signature MUST keep working without a `TypeError`.

This is a bug fix plus an additive protocol extension. It corrects a pre-existing
defect that affects HTTP/REST custom domains and ACM today, not only WebSocket.

## Problem

Stelvio components parent their Pulumi resources to themselves via
`Component._resource_opts()`. That nesting drives URN structure, aliases,
deletion ordering, and the mental model that "everything this component creates
lives under it."

DNS record creation goes through the `Dns` protocol:

```python
class Dns(Protocol):
    def create_record(self, resource_name, name, record_type, value, ttl=1) -> Record: ...
    def create_caa_record(self, resource_name, name, record_type, content, ttl=1) -> Record: ...
```

Neither method accepts `ResourceOptions`. Consequently:

1. **Records land at the stack root.** ACM DNS validation records and API Gateway
   domain CNAMEs are registered as top-level resources even though certificate /
   domain resources are correctly parented.
2. **R11-style parenting contracts are incomplete.** Domain components claim to
   own their resource tree, but the public DNS records they create are orphans in
   the Pulumi graph.
3. **A naive `opts=` addition breaks custom providers.** Unconditionally passing
   `opts=` to every `Dns` implementation raises
   `TypeError: unexpected keyword argument 'opts'` for adapters written against
   the historical signature. Because `AcmValidatedDomain` is shared by HTTP API
   domains (and other certificate users), that regression would hit existing apps
   that never touched WebSocket.

The defect is framework-wide: any component that calls `context().dns.create_*`
without parenting shares it (`RestApi` custom domains, Email DKIM/DMARC,
CloudFront, Cognito custom domains, AppSync, and the shared API Gateway v2 domain
helper).

## Goals

1. Allow DNS record creation to receive `ResourceOptions`, so callers can set
   `parent` (and, if needed, `depends_on`, providers, etc.).
2. Parent ACM validation records and API Gateway v2 domain DNS records to their
   owning Stelvio component.
3. Preserve compatibility with existing user-defined `Dns` implementations that
   do not accept `opts`.
4. Update built-in Route 53 and Cloudflare providers, plus shared test mocks, to
   accept and forward `opts`.
5. Provide a single helper for "pass `opts` only when supported" so call sites do
   not reimplement signature inspection.
6. Remain additive for apps that use built-in providers: same record content,
   same names; only Pulumi parenting/URN placement changes.

## Non-goals

- Changing DNS record content, TTL defaults, or provider selection.
- Requiring every custom `Dns` implementation to accept `opts` immediately.
- A breaking redesign of the `Dns` protocol (e.g. replacing methods with a
  builder, or mandating keyword-only APIs for all parameters).
- Parenting records created outside Stelvio components (raw Pulumi usage).
- Migrating every DNS call site in the same change set (see phased adoption
  below). The protocol and helper MUST land first; high-value shared call sites
  MUST adopt in the same PR; remaining call sites SHOULD follow.

## Users and use cases

- A platform owner inspects the Pulumi resource tree for an `ApiDomain` /
  `AcmValidatedDomain` and sees validation and alias records nested under the
  domain component.
- An application with a custom `Dns` adapter continues to deploy HTTP API custom
  domains after the upgrade without code changes.
- A component author creating DNS records on behalf of a component passes
  `opts=self._resource_opts()` through the shared helper and gets parenting when
  the configured provider supports it.
- Operators relying on Pulumi aliases / replace behavior for domain components
  see DNS children participate in the component's lifecycle consistently with
  certificates and API Gateway domain resources.

## Requirements

### R1. Optional `opts` on built-in providers

`Route53Dns` and `CloudflareDns` MUST accept an optional keyword-only argument:

```python
opts: ResourceOptions | None = None
```

on both `create_record` and `create_caa_record`, and MUST forward it to the
underlying Pulumi record resource.

Test / mock DNS adapters used by the shared AWS test harness MUST do the same.

### R2. Legacy protocol compatibility

The public `Dns` protocol typing MAY document `opts` as optional for new
implementations, but runtime call sites MUST NOT assume every object satisfying
`Dns` accepts the keyword.

Stelvio MUST provide a helper (prototype name:
`_call_with_optional_resource_options(method, *, opts, **kwargs)`) that:

1. Inspects `method`'s signature;
2. Passes `opts=opts` when the signature has a parameter named `opts` **or**
   accepts `**kwargs`;
3. Otherwise calls `method(**kwargs)` without `opts`.

Signature inspection failures (`TypeError` / `ValueError` from `inspect.signature`)
MUST fall back to the no-`opts` call path rather than crashing.

### R3. ACM validation records are parented

`AcmValidatedDomain` MUST create its DNS validation record through the
compatibility helper, passing `opts=self._resource_opts()` (or equivalent options
that parent the record to the ACM component).

This fixes parenting for every consumer of `AcmValidatedDomain`, including HTTP
API domains and any future WebSocket / shared domain component.

### R4. API Gateway v2 domain DNS records are parented

The shared API Gateway v2 domain builder (HTTP and any WebSocket domain that
shares it) MUST create the public alias/CNAME record through the compatibility
helper with the owning domain component's resource options.

### R5. Phased adoption for other call sites

Other in-tree callers SHOULD migrate to the helper + parenting as follow-ups.
At minimum, the following are known stack-root DNS creators today and SHOULD be
tracked:

| Call site | Records |
|---|---|
| `RestApi` custom domain | API alias CNAME |
| `Email` (SES) | DKIM CNAMEs, DMARC TXT |
| `CloudFront` / `Router` | Distribution aliases |
| `Cognito` user pool domain | Custom domain record |
| `AppSync` | Custom domain record |

Migrating them is not a blocker for landing R1–R4, but leaving them unparented
SHOULD be called out in the changelog as known follow-up work.

### R6. No behavior change for record data

For built-in providers, record name, type, value/content, TTL, and zone targeting
MUST remain unchanged. The observable change is Pulumi graph placement (parent /
URN), not DNS payload.

### R7. Additive rollout

- Existing apps using Route 53 or Cloudflare: deploy succeeds; resource URNs for
  affected DNS records may change parent. Document this as a Pulumi graph
  nesting fix.
- Existing apps with custom `Dns` adapters lacking `opts`: deploy succeeds;
  records remain unparented (same as today) until the adapter opts in.
- No required changes to `stlv_app.py` DNS configuration.

## Codebase impact

### Direct changes

| Area | Effect |
|---|---|
| `stelvio/dns.py` | Add `_call_with_optional_resource_options`; optionally document `opts` on the protocol for new implementers. |
| `stelvio/aws/dns.py` | `Route53Dns.create_record` / `create_caa_record` accept and forward `opts`. |
| `stelvio/cloudflare/dns.py` | Same for Cloudflare. |
| `stelvio/aws/acm.py` | Validation record created via helper with component resource options. |
| `stelvio/aws/api_gateway/_domain.py` (or equivalent shared domain helper) | Public domain DNS record created via helper with component resource options. |
| `tests/aws/pulumi_mocks.py` | Mock DNS provider accepts `opts`. |
| URN / parenting tests | Assert validation and domain DNS records are children of the owning component when using built-in providers; assert legacy adapters without `opts` still succeed. |

### Effects across the codebase

- **All ACM-backed custom domains** gain correct validation-record parenting in
  one change (`ApiDomain`, future WebSocket domains, any other
  `AcmValidatedDomain` user).
- **HTTP API domains** fix a pre-existing incomplete resource tree without an
  HTTP-specific patch.
- **Custom DNS providers** stay compatible; adopters can add `opts=` when they
  want parenting.
- **Pulumi state / URNs:** nesting changes may show as resource moves or
  replacement candidates depending on Pulumi version and aliases. The
  implementation SHOULD rely on Stelvio's existing component alias behavior where
  applicable and call out URN parent changes in the PR/changelog so operators are
  not surprised by a large "delete + create" diff that is only a reparent.
- **Consistency pressure:** once the helper exists, unparented call sites
  (`RestApi`, Email, CloudFront, Cognito, AppSync) become obvious follow-ups and
  can adopt the same one-line pattern.

### Out of scope coupling

Do not gate this PR on WebSocket API. The defect and the fix are independent of
protocol type. WebSocket (or a rewrite of it) simply becomes another caller of the
already-parented shared domain helper.

## Design notes

### Capability detection vs protocol break

Two alternatives were considered in the prototype reviews:

1. **Breaking:** add required `opts` to `Dns` and update every implementation.
2. **Capability-detected:** pass `opts` only when the callable supports it.

(1) is cleaner long-term but violates the additive-rollout rule for a public
extension point apps already implement. (2) fixes first-party parenting immediately
and lets third-party adapters opt in. This PRD chooses (2), with documentation
encouraging new adapters to accept `opts`.

### Why a shared helper?

Call sites should not each reimplement `inspect.signature`. Centralizing the
policy keeps "what counts as supporting `opts`" consistent (`opts` parameter or
`**kwargs`) and gives one place to harden edge cases (builtins, bound methods,
wrappers).

### Parent vs full `ResourceOptions`

Passing full `ResourceOptions` (via `_resource_opts()`) rather than a bare
`parent=` keeps DNS creation aligned with every other Stelvio resource: same
provider attachment, same parent, same future options the component centralizes.

### Prototype reference

The `feature/ws-api` branch implements this contract in `stelvio/dns.py`, the
Route 53 / Cloudflare providers, `stelvio/aws/acm.py`, and
`stelvio/aws/api_gateway/_domain.py`. Treat that as the reference prototype for
this spec.

## Validation

Tests MUST cover at least:

- Route 53 / Cloudflare (or mocks with the same signature) receive `opts` and
  parent the Pulumi record under the owning component (URN parent assertions for
  ACM validation and API Gateway domain records);
- a legacy DNS adapter whose `create_record` / `create_caa_record` omit `opts`
  still succeeds when ACM / domain code paths run (no `TypeError`);
- an adapter that accepts `**kwargs` receives `opts`;
- record name/type/value characterization for ACM and API Gateway domain paths
  remains unchanged aside from parenting;
- existing DNS unit/integration suites for HTTP domains and ACM remain green.

## Rollout

1. **PR: DNS parenting (this PRD)** — protocol helper, built-in providers, ACM +
   shared API Gateway v2 domain call sites, regression tests for legacy adapters.
2. **Follow-up PRs** — parent remaining call sites (`RestApi`, Email, CloudFront,
   Cognito, AppSync) using the same helper.
3. Changelog: bug fix for DNS record parenting; note custom-provider compatibility
   and any expected Pulumi URN parent diffs for built-in providers.
