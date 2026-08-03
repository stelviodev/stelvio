# PRD: DNS protocol — require `opts` (breaking)

Status: Draft

Target: `stelvio.dns.Dns` protocol and every implementation / call site;
remove capability-detection scaffolding from
[dns-parenting.md](dns-parenting.md)

Origin: follow-up to [dns-parenting.md](dns-parenting.md). That PRD chose
alternative **(2) Capability-detected** (`_call_with_optional_resource_options`)
so custom adapters without `opts` kept working. This PRD migrates to
alternative **(1) Breaking:** put `opts` on the `Dns` protocol and update every
implementation. A deprecation / dual-support phase is explicitly **not** desired.

## Summary

Make Pulumi `ResourceOptions` (notably `parent`) a first-class, required part of
the public `Dns` contract. Call sites pass `opts=` directly to
`create_record` / `create_caa_record`. Delete the capability-detection helper and
all tests / docs that exist only to preserve legacy adapters without `opts`.

Custom DNS adapters that omit `opts` will raise `TypeError` after this change —
that is intentional and MUST be called out as a breaking change. No shim, no
grace period, no dual code path.

This change also finishes the parenting migration left open by the prior PRD
(R5): every in-tree DNS call site MUST pass `opts=self._resource_opts()` (or
equivalent) so records parent under the owning component.

## Problem

The capability-detected approach landed successfully:

- Built-in Route 53 / Cloudflare / `MockDns` accept and forward `opts`.
- ACM validation records and `ApiDomain` public DNS records are parented via
  `_call_with_optional_resource_options(..., opts=self._resource_opts())`.
- Legacy adapters without `opts` still work because the helper inspects
  signatures and omits the keyword.

That leaves ongoing cost:

1. **Two contracts.** The typed `Dns` protocol still omits `opts`, while
   first-party providers and Stelvio call sites behave as if `opts` exists.
   Structural typing and docs disagree with runtime reality for new adapters.
2. **Helper forever.** Every new DNS call site must remember the helper instead
   of calling the provider. Signature inspection (`inspect.signature`,
   `**kwargs` detection, failure fallbacks) is permanent complexity for a
   one-time compatibility concern.
3. **Incomplete parenting.** RestApi, Email, CloudFront/Router, Cognito, and
   AppSync public records remain stack-root orphans (prior R5). Migrating them
   through the helper spreads the dual-contract further.
4. **False safety.** Apps with custom adapters get silent unparented records
   instead of a clear break that forces them onto the real contract.

The prior PRD already named (1) as cleaner long-term. This PRD takes that path
now that first-party parenting for ACM / ApiDomain is in place.

## Goals

1. Put keyword-only `opts: ResourceOptions | None = None` on the public `Dns`
   protocol for both `create_record` and `create_caa_record`.
2. Call `dns.create_*(..., opts=...)` directly everywhere — no capability
   detection.
3. Delete `_call_with_optional_resource_options` and every import / call of it.
4. Delete tests and docs that assert legacy-without-`opts` or `**kwargs`-only
   adapter compatibility via the helper.
5. Parent every remaining in-tree DNS call site under its owning component.
6. Keep DNS record payloads unchanged (name, type, value/content, TTL, zone).
7. Document a hard breaking change for custom `Dns` adapters; no deprecation
   window.

## Non-goals

- A deprecation phase, compatibility shim, or dual protocol.
- Keeping `_call_with_optional_resource_options` “just in case.”
- Changing DNS record content, TTL defaults, or provider selection.
- Redesigning the `Dns` API beyond adding `opts` (no builder, no rename of
  `create_caa_record`, no keyword-only rewrite of positional parameters).
- Parenting records created outside Stelvio components (raw Pulumi usage).

## Users and use cases

- A platform owner sees DNS records nested under every Stelvio component that
  creates them (ACM, ApiDomain, RestApi, Email, CloudFront/Router, Cognito,
  AppSync) — consistent with certificates and other child resources.
- A component author writes `dns.create_record(..., opts=self._resource_opts())`
  with no helper and no signature guessing.
- An application author with a custom `Dns` adapter updates
  `create_record` / `create_caa_record` to accept `*, opts: ResourceOptions | None = None`
  and forward it (or ignore it deliberately). Deploy fails loudly until they do.
- Operators using only built-in Route 53 / Cloudflare see no app-code change;
  remaining unparented first-party records move under their components (URN
  parent diffs as before).

## Requirements

### R1. `opts` on the `Dns` protocol

Both protocol methods MUST include keyword-only:

```python
*, opts: ResourceOptions | None = None
```

Example shape:

```python
class Dns(Protocol):
    def create_record(
        self,
        resource_name: str,
        name: str,
        record_type: str,
        value: Input[str],
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> Record: ...

    def create_caa_record(
        self,
        resource_name: str,
        name: str,
        record_type: str,
        content: str,
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> Record: ...
```

Default `None` is allowed so adapters may ignore parenting, but the **parameter
MUST exist**. Implementations that omit it are not valid `Dns` providers and
WILL break when Stelvio passes `opts=`.

Docstrings MUST state that providers SHOULD forward `opts` to the underlying
Pulumi record resource. Remove wording that treats `opts` as an optional
extension for “new” providers only.

### R2. Remove capability-detection remains (prior alternative 2)

MUST delete, with no replacement shim:

| Remains | Action |
|---|---|
| `stelvio.dns._call_with_optional_resource_options` | Delete function and unused imports (`Callable`, `Parameter`, `signature` if unused). |
| Call sites using the helper (`stelvio/aws/acm.py`, `stelvio/aws/api_gateway/domain.py`, and any others) | Replace with direct `dns.create_*(..., opts=self._resource_opts())`. |
| `tests/dns/test_dns.py` helper / capability-detection tests | Delete or rewrite so they no longer target the helper (empty module only if something else belongs there). |
| Legacy-without-`opts` adapter tests | Delete (e.g. ACM / ApiDomain tests that wrap `MockDns` in `LegacyDns` omitting `opts` and assert no `TypeError`). |
| `**kwargs`-only adapter tests that exist solely to prove the helper forwards `opts` | Delete or replace with a normal provider that declares `opts`. |
| Changelog / docs language that custom adapters without `opts` “keep working unchanged” | Replace with breaking-change guidance. |

After this PR, `rg _call_with_optional_resource_options` MUST return no matches
in the repo (except historical PRD text in `dns-parenting.md`).

### R3. Built-in and mock providers stay opts-aware

`Route53Dns`, `CloudflareDns`, and `MockDns` already accept and forward `opts`.
They MUST remain aligned with the protocol signature (keyword-only `opts` with
default `None`). No behavioral change required beyond any signature/doc sync.

### R4. Direct `opts` at every Stelvio call site

Every in-tree `create_record` / `create_caa_record` call MUST pass
`opts=<owning component>._resource_opts()` (or equivalent `ResourceOptions`
that parent under that component). This includes sites already migrated via the
helper and the prior R5 leftovers:

| Call site | Records | Notes |
|---|---|---|
| `AcmValidatedDomain` | Validation record | Switch helper → direct `opts=` |
| `ApiDomain` | Public alias/CNAME | Switch helper → direct `opts=` |
| `RestApi` custom domain | API alias CNAME | Parent + pass `opts=` |
| `Email` (SES) | DKIM CNAMEs, DMARC TXT | Parent + pass `opts=` |
| `CloudFront` / `Router` | Distribution aliases | Parent + pass `opts=` |
| `Cognito` user pool domain | Custom domain record | Parent + pass `opts=` |
| `AppSync` | Custom domain record | Parent + pass `opts=` |

Test mocks / stubs that implement `Dns` (including `unittest.mock.Mock` setups
that assert `create_record` call counts) MUST still satisfy the new signature
when exercised through production call paths, or be updated so assertions pass
`opts` through.

### R5. No behavior change for record data

Record name, type, value/content, TTL, and zone targeting MUST remain unchanged.
Observable changes are: (a) protocol / call contract, (b) Pulumi graph parenting
for previously unparented first-party call sites.

### R6. Breaking rollout — no deprecation

- **No** transitional helper.
- **No** “try with opts, fall back without.”
- **No** versioned dual protocol.
- Changelog MUST list a **Breaking Change**: custom `Dns` implementations must
  accept keyword-only `opts: ResourceOptions | None = None` on
  `create_record` and `create_caa_record`. Show a minimal before/after snippet.
- Apps using only built-in Route 53 / Cloudflare need no adapter code changes;
  they MAY see additional URN parent diffs for newly parented call sites.

## Codebase impact

### Direct changes

| Area | Effect |
|---|---|
| `stelvio/dns.py` | Add `opts` to `Dns` protocol; delete `_call_with_optional_resource_options` and helper-only imports. |
| `stelvio/aws/acm.py` | Direct `dns.create_caa_record(..., opts=self._resource_opts())`. |
| `stelvio/aws/api_gateway/domain.py` | Direct `dns.create_record(..., opts=self._resource_opts())`. |
| `stelvio/aws/api_gateway/rest_api/rest_api.py` | Pass `opts` and parent alias record. |
| `stelvio/aws/email.py` | Pass `opts` and parent DKIM/DMARC records. |
| `stelvio/aws/cloudfront/cloudfront.py` | Pass `opts` and parent alias record. |
| `stelvio/aws/cloudfront/router.py` | Pass `opts` and parent alias record. |
| `stelvio/aws/cognito/user_pool.py` | Pass `opts` and parent custom domain record. |
| `stelvio/aws/appsync/appsync.py` | Pass `opts` and parent custom domain record. |
| `stelvio/aws/dns.py` / `stelvio/cloudflare/dns.py` / `tests/aws/pulumi_mocks.py` | Confirm protocol alignment (already have `opts`). |
| Tests | Remove helper + legacy-compat suites; add/extend URN parenting for newly parented call sites; keep payload characterization. |
| `docs/changelog.md` | Breaking change for custom adapters; bug fix / consistency for remaining DNS parenting. |

### Effects across the codebase

- **One Dns contract.** Type checkers, docs, and runtime agree.
- **Simpler call sites.** Same pattern as every other Stelvio resource:
  `opts=self._resource_opts()`.
- **Custom providers break until updated.** Loud failure beats silent orphan
  records.
- **Full first-party parenting.** Prior R5 gaps close in the same breaking PR
  so the helper is not reintroduced for those migrations.
- **Pulumi URN parent diffs** for newly parented records; rely on existing
  root-stack aliases where applicable and document in the changelog.

### Out of scope coupling

Do not invent a new DNS abstraction. Do not gate on unrelated features
(WebSocket, new providers). This is a contract cleanup plus finishing parenting.

## Design notes

### Why break now

The additive helper was the right first step: fix ACM / ApiDomain without
forcing every custom adapter to change. With that shipped, keeping signature
inspection indefinitely taxes every future call site for a compatibility story
we are ready to end. A single breaking PR is cheaper than a deprecation season
that still carries two code paths.

### Why `opts` defaults to `None` on the protocol

The break is “implementations must accept the keyword,” not “every call must
supply a non-`None` parent.” Default `None` matches built-ins and lets an
adapter acknowledge the contract while ignoring parenting if it truly cannot
forward Pulumi options. Stelvio call sites still pass `_resource_opts()`.

### Why no deprecation phase

Deprecation would mean keeping `_call_with_optional_resource_options` (or an
equivalent try/except) for at least one release — exactly the complexity this
PRD removes. The prior PRD already bought compatibility time. This follow-up
spends it.

### Relation to `dns-parenting.md`

Treat [dns-parenting.md](dns-parenting.md) as historical context for why (2)
shipped first. This PRD supersedes its R2 (legacy helper), R5 (phased call-site
adoption via helper), and R7 (custom adapters without `opts` keep working).
R1/R3/R4/R6 outcomes (built-ins forward `opts`; ACM / ApiDomain parenting;
unchanged payloads) remain in force under the new call style.

## Validation

Tests MUST cover at least:

- Protocol-shaped providers (`Route53Dns`, `CloudflareDns`, `MockDns`) still
  parent records when `opts=` is passed (existing URN assertions stay green).
- ACM validation and ApiDomain public DNS records remain parented after switching
  off the helper (URN assertions; no helper imports).
- Newly migrated call sites (RestApi, Email, CloudFront/Router, Cognito, AppSync)
  parent their DNS records under the owning component (URN assertions) and keep
  existing payload / call-count characterization where present.
- A provider implementation that **omits** `opts` fails when exercised through a
  Stelvio call path that passes `opts=` (`TypeError` / unexpected keyword) —
  proving the break, not papering over it.
- `rg _call_with_optional_resource_options` is empty outside `docs/prd/dns-parenting.md`.
- Existing DNS unit suites for ACM / HTTP domains remain green; update
  integration expectations only if assert helpers care about parenting (they
  should not — parenting remains a Pulumi-graph concern).

## Rollout

1. **Single PR (this PRD)** — protocol `opts`; delete helper and legacy-compat
   tests; direct `opts=` at all in-tree call sites including prior R5 leftovers;
   URN parenting tests for newly parented sites; changelog breaking note.
2. **No follow-up compatibility PR.** If something still calls the helper or
   documents soft `opts`, that is a bug in this PR.
3. Changelog: Breaking change for custom `Dns` adapters; DNS parenting completed
   for remaining first-party call sites; note expected Pulumi URN parent diffs.
