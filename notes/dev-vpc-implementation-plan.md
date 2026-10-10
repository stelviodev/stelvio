# Stelvio VPC dev mode: implementation plan

Version 1.0, 5 October 2026.

## 1. Purpose and authority

Implement the [system specification](dev-vpc-specification.md) and satisfy all
R01–R13 and A01–A14 in the [requirements](dev-vpc-requirements.md). This plan
defines work packages, dependencies, change areas, tests, and completion gates.
It does not report implementation or acceptance results.

The older [dev-vpc-plan.md](dev-vpc-plan.md) and prototype branch are reference
material. They do not override the current requirements or justify retaining a
particular mechanism. The implementation profile deliberately left open by
specification section 10.2 is the first engineering deliverable below.

Live progress, decisions, blockers, and evidence locations belong in
[tasks/dev-vpc.md](../tasks/dev-vpc.md), with one index entry in
[todo.md](../todo.md). Keep this document as the plan, not a running work log.

## 2. House rules applied to this work

### 2.1 Source documents

Read these at the beginning of implementation and again if their contents change:

| Area | Contributor guide | Applicable skill |
| --- | --- | --- |
| Workflow and checks | [CONTRIBUTING.md](../CONTRIBUTING.md), [code style](../docs/docs/contributing/code-style.md) | [architecture](../.agents/skills/architecture/SKILL.md), [task-system](../.agents/skills/task-system/SKILL.md) |
| Components and links | [components](../docs/docs/contributing/components.md) | [writing-components](../.agents/skills/writing-components/SKILL.md), [link-system](../.agents/skills/link-system/SKILL.md) |
| Unit tests | [unit tests](../docs/docs/contributing/unit-tests.md) | [writing-tests](../.agents/skills/writing-tests/SKILL.md) |
| Real AWS tests | [integration tests](../docs/docs/contributing/integration-tests.md) | [writing-integration-tests](../.agents/skills/writing-integration-tests/SKILL.md), [running-integration-tests](../.agents/skills/running-integration-tests/SKILL.md) |
| User documentation | [documentation](../docs/docs/contributing/docs.md) | [docs](../.agents/skills/docs/SKILL.md), [writing-documentation](../.agents/skills/writing-documentation/SKILL.md), [changelog](../.agents/skills/changelog/SKILL.md) |
| Reviews | [code style](../docs/docs/contributing/code-style.md), test guides above | [review-code](../.agents/skills/review-code/SKILL.md), [review-tests](../.agents/skills/review-tests/SKILL.md) |

Two details need care when combining the sources:

- The component skill explicitly makes `Resources` fields and customization keys
  independent. The public guide describes them as mirrored. Preserve the narrow
  public surface and use explained exclusions in the existing shared sync test;
  do not expose plumbing solely to satisfy a test.
- The integration-running skill's tier examples omit the newer VPC tier.
  `tests/integration/run_all.sh`, its collection hooks, and the contributor guide
  define the current tier behavior. The new exclusive macOS lane below is an
  intentional extension of that runner, not a return of VPC tests to the standard tier.

### 2.2 Source conventions

1. Use Python 3.12-compatible syntax, full annotations, modern unions, and Ruff's
   configured 99-character limit. Prefer small module-level pure functions where
   no instance state is needed. Explain ordering, ownership, and platform quirks
   with why-comments; do not add unexplained lint suppressions or inline imports.
2. Validate and normalize constructor input in `__init__`, using descriptive
   `ValueError`/`TypeError` messages. Keep configs frozen, components and Resources
   `@final`, and instance configuration assigned in the constructor.
3. Keep `_create_resources()` lazy and free of instance-state mutation. It reads
   normalized configuration and returns frozen Resources. Runtime session state
   belongs to the coordinator, not to a VPC component mutated after deployment.
4. Use `_resource_opts(...)` for component-owned Pulumi resources and existing
   provider/parent/alias handling. Route meaningful resource arguments through
   the established customizer, with explicit tag injection on taggable resources.
   Preserve the customization escape hatch; do not invent general validation of
   customized provider arguments. Apply `fixed=` only for the skill's documented
   constructor-derived consistency case.
5. Use `resource_name` with the destination's limit and the correct provider
   suffix allowance. Do not introduce another naming helper or deterministic
   physical names where provider-generated names are appropriate.
6. Keep session keys, leases, DNS plumbing, temporary ownership journals, and
   settlement handles off public Resources/configuration surfaces. Preserve
   useful existing bastion resource access and customization API unless an
   intentional API change is separately agreed.
7. New component-owned role permissions follow the inline `RolePolicy` pattern.
   Do not migrate Function's established managed-policy/attachment API as part
   of this work. Keep developer, bastion, and Lambda permissions separate and
   least-privilege. Preserve link override precedence, generated env vars, and
   existing secret/CA behavior.
8. Do not introduce a public Bastion component or a generic networking plugin
   framework merely to organize implementation code. If a new registered
   component becomes necessary, apply the complete component checklist:
   exports, four shared test suites, customization/tagging, URNs, resource display
   names, and data-loss warnings only for genuinely persistent data.

### 2.3 Testing and delivery conventions

- Drive component behavior through constructors and `.resources`, including
  config validation. For runtime code, use the highest practical CLI/session or
  helper-protocol boundary; assert effects and responses, not private fields or
  the number of calls to an implementation function.
- Use pytest functions, fixtures, direct imports such as `mark`, `param`, and
  `raises`, and parametrization for actual input variations. Match exception
  messages and compare parsed structures exactly; avoid substring assertions.
- Resource-driving unit tests take `pulumi_mocks`. Use `@pulumi.runtime.test`;
  assert recorded inputs/counts only after a decorated inner deployment returns.
  Keep every `apply` inside the decorated scope. For output assertions, return
  the Output chain and include all outputs the check reads.
- New resource assertions use `assert_res`, `assert_res_counts`, and
  `assert_no_res`, with resource types and relationships checked and total counts
  sealed. Do not add `created_*` queries, hand-built settlement barriers, or
  production Resources fields solely for tests.
- Preserve hermetic unit fixtures: no developer AWS config, real downloads,
  actual sudo, or workstation network changes in the default unit suite.
- Before deleting or replacing a prototype test as redundant, prove the public
  behavioral replacement detects a deliberate regression; restore the mutation
  immediately. Coverage alone is not evidence of that replacement.
- Follow the writing-tests delivery rule: implement one test, explain/run it,
  and allow review before adding the next. Do not generate a whole suite in one
  write. Previously agreed user authorization can change the interaction cadence.
- Review source and tests separately using their respective reviewer agents;
  run those reviews sequentially because they share the checkout. Evaluate
  findings against the specification rather than applying them blindly.

Public docs, changelog, branch creation, commits, and pushes retain their skill
workflows. Use existing authorization when present; do not repeat an approval
already given. This plan does not itself authorize deployments, privileged host
changes, commits, or publication. No such action is needed to write the plan.

## 3. Baseline and change map

Planning inspected `feature/documentdb-vpc` at `8045dfc3`. Recheck the working tree
and branch before implementation; do not reset unrelated or untracked work.
Read other references with `git show`, not by switching the shared checkout.

| Area | Existing files | Planned responsibility |
| --- | --- | --- |
| Public policy and persistent infrastructure | `stelvio/aws/vpc.py`, `stelvio/aws/bastion.py`, `stelvio/aws/document_db.py` | Normalize all bastion forms; preserve component conventions; distinguish persistent resources from session-owned access. |
| Metadata and dependencies | `stelvio/aws/dev_network.py`, `stelvio/aws/function/function.py`, `stelvio/stack_outputs.py` | Describe all used VPCs, resources, endpoint dependencies, and custom DNS ownership without secret leakage. |
| Runtime and host code | `stelvio/dev/` | Replace/move appropriate responsibilities into `stelvio/tunnel/`; preserve behavior only when it meets the specification. |
| Commands and session lifetime | `stelvio/cli/__init__.py`, `stelvio/cli/commands.py`, `stelvio/cli/tunnel.py` | Auto-only mode, install/uninstall, orchestration, partial failure, diagnostics, and cleanup. |
| Deployment state integration | `stelvio/command_run.py`, `stelvio/state_ops.py`, `stelvio/aws/home.py`, `stelvio/provider.py`, `stelvio/pulumi.py` | Reuse existing state/provider/CLI setup where suitable; add independent temporary-resource ownership without changing application mode on teardown. |
| Invocation admission | `stelvio/bridge/local/listener.py`, existing handler dispatch and Function dev execution | Gate only affected endpoints; preserve serialization and current invocation/result behavior. |
| Unit/regression tests | `tests/dev/`, `tests/aws/test_vpc_bastion.py`, `tests/aws/test_dev_network.py`, `tests/aws/test_document_db.py`, `tests/aws/function/`, `tests/bridge/`, CLI/state tests | Replace implementation-bound prototype coverage with behavior at maintained boundaries; relocate runtime tests to `tests/tunnel/`. |
| Integration harness | `tests/integration/stelvio_test_env.py`, `conftest.py`, `export_helpers.py`, `cleanup.py`, `run_all.sh` | Actual CLI lifecycle, durable cleanup, an exclusive macOS lane, and exact AWS assertions. |
| Example and real scenarios | `spikes/vpc-tunnel-app/`, `tests/integration/test_dev_mode.py`, `test_document_db.py` | Prove the actual default dev command, TLS, discovery, read/write, and temporary teardown. |
| Packaging and CI | `pyproject.toml`, `uv.lock`, `.github/workflows/integration-tests.yml` | Ship the independent helper artifact and add a real macOS acceptance execution path. |

The prototype contains contradictory historical forwarding terminology: its
`plans.py` discusses packet-filter interception and also records a switch to
utun routing. Treat this as a reason to inspect and prove the actual packet path,
not as proof that either backend meets acceptance. Likewise, the existing
skipped VPC-dev test is not an end-to-end test.

Two further baseline constraints must not get lost during the rewrite:

- `Vpc` has fixed default addressing and no `cidr=` constructor argument in the
  inspected code. The multi-VPC proof must use the existing customization hooks
  to change the VPC CIDR and every affected subnet tier consistently. Read the
  deployed/customized ranges in manifests and resolver/bootstrap configuration.
  Do not quietly invent a new public CIDR API or merely customize the VPC while
  leaving its subnets in the original range. If existing customization cannot
  satisfy the proof, resolve that API gap explicitly before dependent work.
- The prototype strips environment credentials and removes credential providers
  from its resolver. Do not inherit that restriction. Isolation from handler
  mutation must preserve the developer's supported credential source, including
  startup environment credentials used by CI, without copying secrets into
  durable metadata, helper requests, or logs.

## 4. Implementation strategy and decision gates

Keep the existing invocation bridge. Build networking as a separately owned
runtime with a connection per enabled VPC, a host-level helper lease, and
per-endpoint admission. Do not add broader bridge reconnection, queueing, worker
isolation, or cancellation redesign unless a demonstrated acceptance failure
requires a narrowly scoped change.

Use a nonprivileged supervisor process separate from handler execution. This
keeps credential-provider configuration, health checks, and transport lifetime
outside handler reload and temporary-environment scopes. The trusted privileged
helper owns only OS networking and local ownership records.

Persistent access resources remain in the normal application deployment.
Temporary access resources need an independently removable ownership unit for
each enabled `None` VPC. The preferred candidate is a separate internal Pulumi
deployment/state namespace per session/VPC using existing Home/provider
mechanisms. Prove that choice in P0 before production changes: it must not depend
on rerunning the user application or exporting/mutating its main stack state.
Separate processes must isolate Pulumi registries and AppContext where needed.

P0 closes the following engineering decisions with an implementation profile:

| Decision | Required deliverable and proof |
| --- | --- |
| Helper distribution | A self-contained artifact with a trusted runtime outside all venvs; exact installation inventory, permissions, bootstrap, upgrade/refusal, and uninstall procedure. Demonstrate operation after deleting the installing venv. Select the packaging tool based on that proof. |
| macOS TCP path | One selected forwarding backend with a complete local-socket → correct VPC → resource proof. Include destination recovery/dispatch and safe teardown; do not choose from filenames or retain multiple speculative backends. |
| DNS integration | Exact macOS resolver integration and VPC DNS path, proven against an actual private zone, TCP/UDP resolver clients, and outage rejection without public fallback. |
| Temporary AWS state | Namespace, locking, stable ownership identity, crash journal, deletion/reconciliation, and recovery when the original venv is gone. Prove application resources and main deployment state remain intact. |
| SSH authentication | Concrete client-authentication method and trusted host-identity bootstrap, including reconnect and changed-identity rejection; no public SSH ingress. |
| Runtime support and bounds | Tested macOS/architecture and Python matrix; pinned helper/dependency/AMI selection; numeric startup, probe, health, backoff, and cleanup limits with reasons and observable failure behavior. |

Record the selected design, alternatives rejected, evidence, and remaining
limitations in the active task's linked implementation-profile note. An
unproved technical assumption is a failed gate, not an implicit scope reduction.
Revisit the mechanism when a proof fails; ask the user only if product scope,
public API, cost constraints, or previously agreed behavior must change.

## 5. Work packages

Each package includes its behavioral tests and review. Do not postpone ordinary
testing until the end-to-end package. File names for new private modules are
organization suggestions; avoid a file per trivial type or a duplicate model
layer merely to match this document.

### P0 — Establish the implementation profile and prove the risky boundaries

**Depends on:** the agreed requirements/specification.

**Work:**

1. Record baseline behavior and relevant test results. Map each prototype area
   to retain, adapt, or replace, with its observable behavior as the reason.
2. Define the artifact inventory and privilege boundary. Prototype install,
   compatibility handshake, invocation from a second venv, removal of the first
   venv, active-session refusal, and full uninstall on a controlled macOS host.
3. Prove the chosen transparent TCP backend through SSH over SSM with original
   hostnames and verified server identity. Exercise two distinct-CIDR VPCs using
   coherent VPC/subnet customization through the existing public API;
   explicit local port forwarding alone does not prove transparency.
4. Prove a real private hosted-zone answer through the actual OS resolver and
   verify unrelated DNS, outage rejection, restoration, and per-VPC selection.
5. Prove independent creation/deletion of a temporary access unit and its
   ingress rules, including partial creation and interrupted state recording.
6. Select the concrete implementation profile described in section 4. Give
   every temporary proof resource an owner and teardown path from the outset.

These proofs are implementation work performed when execution is requested;
they are not AWS runs already authorized or performed by this planning task.
Before a live run, use the integration-running skill to obtain the AWS profile
if it has not been supplied. Do not copy a profile or another developer's
absolute path from a skill example.

**Exit gate G0:** selected packaging, forwarding, DNS, authentication, and AWS
ownership mechanisms each have recorded proof and cleanup results. No dependence
on a user-writable Python runtime remains in privileged execution. No production
migration depends on an unresolved mechanism.

### P1 — Introduce the tunnel package, policy, and resolved contracts

**Depends on:** G0 for the production runtime contracts.

**Change areas:** `stelvio/tunnel/` (new), AWS metadata adapters, VPC constructor,
stack-output handling, matching runtime/component unit tests.

1. Create the `stelvio.tunnel` package with typed, frozen value records and
   explicit policy/state enums. Keep mutable lifecycle owners distinct from
   immutable input records. Use the public constructor to test normalization.
2. Normalize omitted/`None` to temporary, `True` and config/dictionary forms to
   persistent, and `False` to disabled. Preserve `BastionConfig`, dictionary
   input, empty config behavior, and non-empty `dns_domains`.
3. Replace the single-VPC manifest with a collection keyed by stable VPC
   identity. Map every bridge endpoint to its enabled VPC dependencies; retain
   disabled VPCs for warnings, not for readiness blocking.
4. Publish/collect resolved network and supported-resource metadata privately,
   with ports, CIDRs, hostnames, access targets, custom DNS domains, and provider
   context. Preserve ordinary component outputs and URL-oriented CLI output;
   do not expose this as a new public output API.
5. Validate malformed/duplicate identities, unsupported versions, unresolved
   references, ports, ranges, and DNS ownership before dependent changes.
   Validate entire VPC CIDRs for overlaps, not just the first selected subnet.
6. Migrate import users coherently. Do not mechanically copy every prototype
   module/test. Temporary migration adapters, if needed between work packages,
   have an explicit removal point at P5; the finished runtime uses `stelvio.tunnel`.

**Verification:** parameterized policy inputs including empty dictionaries and
wrong types; full resource/link behavior through public APIs; multi-VPC metadata
consumed through the output/CLI boundary; visible validation diagnostics; no
secret-bearing metadata or regression to non-VPC application behavior.

**Exit gate G1:** normalized policy and multi-VPC contract are usable by AWS
provisioning, helper, and session work; component conventions and targeted
behavioral tests pass. R02/R11/R13 contracts are represented without a new public
networking configuration system.

### P2 — Implement persistent and temporary AWS access lifecycles

**Depends on:** G0, G1.

**Change areas:** VPC/bastion/DocumentDB integration, private AWS lifecycle module,
state integration, component tests, AWS lifecycle integration fixtures.

1. Keep persistent bastion resource creation in the normal lazy component path.
   For `None`, emit the information needed by the later access unit, without
   attaching a permanent bastion to the application deployment. For `False`,
   create neither path and emit the specified warning for each command.
2. Implement the P0-selected temporary access owner. Persist creation intent
   before mutations; reconcile the create-succeeded/recording-interrupted
   window; record exact identities and deletion dependencies durably.
3. Create a dedicated access security group and only required service-port
   ingress. Keep normal workload rules and custom Function SGs intact. Ensure
   the temporary access unit owns its ingress rule, not the database itself.
4. Resolve target IDs/ports from deployment metadata. Do not reopen or mutate
   already-created component instances to attach runtime resources. Reuse
   resource-building logic where it preserves distinct ownership boundaries.
5. Preserve SSM-only SSH access, correct outbound connectivity, trusted identity
   bootstrap, and appropriate instance/provider permissions from the profile.
6. Implement idempotent teardown/recovery for partial creation/deletion, missing
   resources, expired credentials, and ownership conflicts. Preserve recovery
   records when APIs fail. Coordinate with relevant state locks and never write
   a stale copy of the main application state.
7. Check ordinary deploy/dev/destroy transitions and existing explicit bastion
   deployments. Avoid unintended replacement of VPCs, databases, or unrelated
   resources; retain aliases/names where the logical resource is unchanged.

**Verification:** Pulumi behavioral tests with exact counts/relationships and
negative assertions; real create/stop/delete proof for `None`; persistence for
`True` and config forms; unchanged workload ingress; recovery after partial
failure; no application-mode change or state drift on dev teardown.

**Exit gate G2:** A03/A04 and AWS portions of A08 are demonstrated. Every
temporary resource, including SG rules and supporting identities/documents, has
an owner, cleanup path, and recoverable failure record.

### P3 — Ship the global macOS helper and lifecycle operations

**Depends on:** G0, G1.

**Change areas:** helper/installation modules under `stelvio/tunnel`,
`stelvio/cli/tunnel.py`, packaging metadata/build artifacts, helper tests.

1. Build and distribute the selected trusted helper artifact. Verify both wheel
   and source-distribution contents and behavior; no development-checkout path,
   editable-install reference, or first-project interpreter may be required.
2. Implement inspect, acquire, configure/remove VPC, release, and reconcile at
   the helper boundary. Authenticate peer identity; fence requests by session
   and connection generation; validate requested endpoints/ranges and bounds.
3. Journal owned host changes and rollback partial operations. Use one atomic
   host lease across all VPCs in a session. Detect stale owners without trusting
   a reused PID or deleting another application's state.
4. Install only after displaying required system changes and obtaining needed
   elevation. Execute privileged code only from the trusted installed runtime;
   never elevate the calling venv interpreter or permit arbitrary command paths.
5. Implement compatibility checks and safe idempotent install. Refuse a
   disruptive upgrade while a session is active.
6. Make cleanup exclude new acquisitions atomically, refuse active sessions,
   reconcile stale state, stop the helper, and remove its dedicated artifacts
   and privileges. Keep unfinished-cleanup and AWS recovery records when needed.
7. Keep all platform-specific imports behind the macOS adapter so ordinary
   library imports and non-VPC tests still run on other platforms.

**Verification:** isolated filesystem/protocol behavior tests plus actual macOS
install/use/uninstall; venv A deleted then venv B works; second session refused;
invalid callers/requests rejected; partial install/cleanup recoverable; foreign
settings preserved; repeated cleanup and reinstall work.

**Exit gate G3:** A01/A09 and helper portions of A10/A11 pass with the built
artifact, not only source-level mocks. Helper uninstallation never destroys AWS.

### P4 — Implement multi-VPC transport, discovery, DNS, and supervision

**Depends on:** G0–G3.

**Change areas:** nonprivileged supervisor, transport/forwarding/DNS adapters,
resource discovery, status messages, and corresponding contract/live tests.

1. Capture provider/profile/region configuration and the selected credential
   source before handler environment scopes. Preserve supported named-profile
   and startup-environment authentication in a protected nonprivileged runtime;
   do not discard a provider solely to avoid reading handler mutations. Refresh
   where the provider supports it, and report expired fixed credentials honestly.
   Keep credential material out of serialized manifests, root-helper messages,
   state, and logs. Run transports, forwarding, and DNS outside the handler
   process's mutable state.
2. Maintain one independently supervised connection per enabled VPC. Configure
   destinations from resolved ranges; select the correct VPC for each socket;
   retain the original hostname, port, TLS verification, and driver behavior.
3. Support automatic DocumentDB endpoint/member discovery. Use the owning
   resource/VPC identity rather than assigning a whole AWS region's service
   suffix to the first connection. Handle refreshed addresses without permanent
   hosts-file mappings.
4. Route declared custom domains to the owning VPC's DNS view. Validate label
   boundaries, same-owner duplicates, and cross-owner domain collisions. Support
   ordinary UDP/TCP resolver requests, aliases, negative answers, and fallback
   after DNS truncation as specified.
5. Make readiness generation-specific and based on authenticated transport,
   local configuration, DNS, and available resource probes. Distinguish tunnel
   verification from resource/authentication verification when no target is known.
6. Implement health detection, capped backoff, interruptible attempts, and
   revalidation on reconnection. Classify recoverable outages separately from
   revoked authorization or changed server identity. Restore new-connection
   availability without replaying handlers or database operations.
7. On terminal VPC failure, remove its transport/forwarding and temporary AWS
   access while preserving healthy VPCs. Retain only the owned DNS rejection
   configuration needed to prevent private queries falling back publicly until
   session exit; keep that retained ownership visible.

**Verification:** two VPCs with distinct resources and custom domains; driver
member discovery with verified TLS; per-VPC transport interruption and recovery;
private DNS loss without public fallback; unrelated DNS/API traffic survives;
generation fencing; named-profile and environment-source authentication;
handler reload/idle/activity do not change tunnel identity.

**Exit gate G4:** networking portions of A02/A06/A07/A10/A11/A12/A14 pass. No
healthy VPC is torn down because another reconnects or fails.

### P5 — Integrate CLI startup, partial failure, admission, and shutdown

**Depends on:** G1–G4.

**Change areas:** CLI/session coordination, bridge admission, state capture,
diagnostics, remaining import migration, and CLI/bridge tests.

1. Accept only `auto`, defaulting to it; reject deferred/invalid modes before
   deployment. Non-VPC and all-disabled sessions bypass helper setup. Remove
   remedies that advise using deferred `external`/`managed` modes.
2. Preserve resolved metadata and provider configuration before `CommandRun`
   releases/cleans its work directory. Start the temporary access and network
   runtime without reevaluating the application merely to discover networking.
3. Distinguish global startup failures from per-VPC failures. Keep the server
   running after a VPC fails; report READY/DEGRADED and individual causes. A
   disabled VPC warns and does not make the session degraded by itself.
4. Gate each invocation on all its enabled VPC dependencies before import or
   execution. Reject affected invocations within their budget through the
   existing bridge. Keep healthy-VPC, non-VPC, and explicit-opt-out handlers
   available. Do not add an unbounded queue or a new concurrent worker model.
5. Wire normal exit, Ctrl+C, handled failures, and stale-session recovery into
   the same idempotent ownership cleanup. A stuck handler cannot block network
   cleanup indefinitely; do not claim safe thread cancellation.
6. Aggregate cleanup failures without skipping other VPCs. Report residual
   identities and exact recovery actions. Preserve application/persistent
   bastion resources and the helper installation after ordinary dev exit.
7. Remove completed migration adapters and obsolete `stelvio.dev` runtime
   dependencies; keep unrelated bridge behavior outside this change.

**Verification:** actual command selection and diagnostics; all policy variants;
partial startup/runtime failure; an endpoint requiring more than one VPC;
non-VPC regression; preserved invocation serialization/deadline behavior;
shutdown with an in-flight handler; installed package imports from `stelvio.tunnel`.

**Exit gate G5:** the default command drives the entire specified lifecycle;
A05/A13 and CLI/admission portions of A02/A06/A08/A14 pass.

### P6 — Complete the real macOS/AWS acceptance suite

**Depends on:** G2–G5; harness foundations can be written with earlier packages.

**Change areas:** integration harness, new VPC tunnel scenario module(s), test
handlers/assert helpers, runner selection, cleanup, and macOS execution job.

Implement the test-harness changes in section 6 and the complete matrix in
section 7. Each live test uses unique ownership and verifies cleanup in `finally`
or fixture teardown. State/configuration inspection supplements observable
behavior; successful deployment alone never satisfies a scenario.

**Exit gate G6:** all A01–A14 have passing evidence on the declared macOS profile,
including private DNS and actual DocumentDB TLS/member discovery. No skipped,
mocked, Linux-only, or direct-relay-only result substitutes for these gates.

### P7 — Documentation, reviews, packaging checks, and delivery

**Depends on:** G5; completion requires G6.

1. Use the docs/changelog workflows with any approval already given. Put the
   full setup/session/recovery workflow in `docs/docs/concepts/dev-mode.md`;
   Describe the shipped Traforo wheel assets, macOS 15+ arm64/x86_64 target,
   separate privileged helper/nonroot forwarder, Session Manager plugin, and
   matching-old-package cleanup before binary upgrades. Distinguish target
   support from native acceptance, including the compiler of the accepted asset.
   Public policy and cost/lifetime behavior belong in
   `docs/docs/components/aws/vpc.md`. Keep the CLI page's command reference in
   `docs/docs/intro/using-cli.md`, with links rather than duplicate guidance.
2. Update `docs/docs/components/aws/document-db.md` for verified local access,
   custom DNS where relevant, unchanged TLS/replica settings, and the correct
   example. Update troubleshooting, contributor testing instructions, and the
   runner documentation for the macOS lane. Add nav entries only for new pages.
3. Explain temporary versus persistent costs and incomplete-cleanup recovery.
   Check current AWS pricing if publishing concrete cost figures; do not copy
   historical numbers. Follow the docs skill's tone, full imports, executable
   examples, and no-em-dash rule for user-facing pages.
4. Remove the stale example comment claiming `bastion=True` is required to opt
   into dev networking. Preserve a `True` case and an omitted-argument case in
   acceptance; keep the example and integration fixture synchronized.
5. Run source review, then test review. Resolve blocking findings and rerun the
   checks affected by the fixes. Verify packaging from clean project venvs,
   including both Traforo architectures, source/artifact coherence, command
   selection, and non-macOS import/non-VPC compatibility.
6. Write a concise changelog entry for the user-visible feature and actual
   migration impact. Run final checks and document unverified limitations
   honestly. Use the branch/commit/push skills only when those actions are
   requested, with their concrete review/authorization steps.

**Exit gate G7:** reviewed source/tests, synchronized docs/example, passing
relevant checks, complete acceptance evidence, and no unresolved blocker against
the specified support claim.

## 6. Integration harness and execution changes

### 6.1 Preserve the repository's AWS test lifecycle

Extend `StelvioTestEnv` or add a small sibling CLI-session fixture using the same
identity, provider, state-retention, and teardown conventions. Do not bolt a
second unrelated AWS deployment onto a test and let the fixture clean only one.
The actual CLI must own the application deployment it creates; the harness must
record that ownership, stop its process, and destroy its application and access
resources through the appropriate state owners on teardown.

Infrastructure-only tests continue using `stelvio_env`, `project_dir`, and
explicit `export_*` helpers. For CLI scenarios, use real command outputs/state
read through a maintained helper to obtain the Function URL and ownership
identities. Do not scrape human prose or expose internal metadata as a new user
API just to make tests easier.

Use the shared `_boto3_session()` path for AWS assertions, extend existing
`assert_document_db.py`/`assert_vpc.py`, and add a small `assert_tunnel.py` for
new service-specific checks rather than growing `assert_helpers.py` or creating
inline boto3 sessions. Integration tests remain plain pytest functions, without
Pulumi mocks, `@pulumi.runtime.test`, or output settlement scaffolding.

Launch a real CLI subprocess, consume readiness without arbitrary sleeps, send
Ctrl+C through its process group, and use bounded teardown escalation with a
retained log/state path. Do not use a macOS-unavailable shell `timeout` command.
Readiness polling and failure injection must exercise production boundaries;
do not add hidden production skip-verification flags for tests.

### 6.2 Add an exclusive, explicitly enabled macOS tunnel lane

Proposed test interface: marker `integration_tunnel` and sole opt-in flag
`--integration-tunnel`. These names are planned additions, not existing commands.

- Register the marker/flag in `pyproject.toml` and integration collection hooks.
  Tunnel cases retain VPC classification where appropriate, but the exclusive
  tunnel selector takes precedence over the normal VPC marker. Other lanes
  must skip these tests, even when selecting all VPC tests.
- Run this lane with `-n 0`: one host, one active tunnel session. Do not multiply
  workers because the ordinary VPC lane permits four. A single multi-VPC case
  consumes several VPCs; verify account/region capacity for the actual fixture.
- Extend `run_all.sh` with an explicit opt-in for the macOS lane. Wait for the
  ordinary VPC lane to finish before running it, so the shared account's VPC
  capacity is not consumed concurrently by those jobs. Recount tests and keep
  worker counts/documentation in this runner, not duplicated in ad hoc scripts.
- Private hosted-zone fixtures create their own disposable zone/records with
  the test VPC. They do not use an unrelated public DNS zone or combine
  `--integration-dns` with the tunnel flag. Existing public-DNS tests retain
  their existing domain/zone prerequisites.
- Require a controlled macOS host with no active developer tunnel and an
  agreed installation baseline. Do not overwrite or uninstall a foreign helper
  installation to prepare a test. Use the real install/cleanup commands and
  approved elevation flow, preserving unrelated host settings.
- Extend the manual-dispatch CI arrangement with a macOS execution path meeting
  that profile, or document an equivalently controlled manual release run.
  The current Ubuntu workflow cannot establish macOS acceptance. Missing live
  capability is reported as unverified, not as a green acceptance gate.

### 6.3 The mandatory example test

Use `spikes/vpc-tunnel-app/stlv_app.py` and its handler, or a synchronized fixture
with explicit checks against drift. Test modifications may provide unique app
identity, the agreed AWS provider, and a local-only execution marker; they must
not change database endpoints, disable TLS/hostname checks, or bypass discovery.

Run the actual `stlv dev` command with no extra parameters. Invoke the deployed
Function URL; compare the parsed response's ping, read-back, host/discovery,
and local-execution evidence. Test `True` and omission/`None` lifetimes, then
interrupt transport and demonstrate automatic recovery without replay.

For multiple VPCs, prove which resource was reached with distinct response/data
markers, not simply two successful TCP connects. Fail each path in turn and
assert the healthy path and non-VPC handler continue. Private DNS cases must use
the OS resolver; querying only the relay does not close the test.

## 7. Acceptance mapping

| Case | Implementation owners | Required final evidence |
| --- | --- | --- |
| A01 Cross-venv helper | P3, P6 | Install from A; delete/move A; run B sequentially without reinstall; correct privilege/runtime origin. |
| A02 Default example | P2, P4, P5, P6 | Real CLI → AWS URL → local handler → verified DocumentDB TLS/member discovery and read/write. |
| A03 Temporary lifecycle | P2, P5, P6 | Omitted/None dev access removed on exit/Ctrl+C; ordinary deploy creates none; application retained. |
| A04 Policy forms | P1, P2, P5 | None/True/False/config/dict/empty-config behavior and warnings; explicit persistence. |
| A05 CLI regression | P5 | Default equals auto; unsupported modes rejected early; non-VPC dev without helper. |
| A06 Reconnection | P4, P5, P6 | Real interruption, closed admission, automatic new connection, successful new invocation, no replay. |
| A07 Runtime isolation | P4, P6 | Reload, idle, and handler activity leave transport and credential-provider identity intact. |
| A08 Cleanup/failure | P2, P3, P5, P6 | Partial startup, Ctrl+C, crash, and failed deletion preserve ownership and allow recovery without touching foreign resources. |
| A09 Uninstall | P3, P6 | Active refusal; complete uninstall from another venv; idempotence and successful reinstall. |
| A10 Boundaries | P1, P3, P4, P6 | Second host session, incompatible helper, and host/VPC conflicts rejected without disruption. |
| A11 Security | P2, P3, P4, P6 | SSM transport, no public SSH, verified identity, non-root runtime, rejected privileged abuse. |
| A12 DNS/prerequisites | P1, P4, P6 | Automatic resource hosts and real custom private DNS, outage rejection, cleanup, unrelated DNS preserved, actionable prerequisites. |
| A13 Package | P1, P3, P5, P7 | Installed code/assets/entry points use `stelvio.tunnel`; package works outside an editable checkout. |
| A14 Multiple VPCs | P1–P6 | Simultaneous distinct-CIDR paths, mixed policies, per-VPC failures/recovery/cleanup, overlap rejection. |

A unit test may close a deterministic argument-validation check. A live
connectivity, privilege, crash-recovery, DNS, or installed-artifact claim requires
the corresponding real boundary to be exercised. Record PASS, FAIL, or NOT RUN
with commands, profile versions, results, and any retained resource identities.

## 8. Check commands and evidence

Run targeted checks during each package, then broaden for shared/runtime changes.
Do not repeatedly rerun already-passing checks without a change or new concern.

Current repository commands, run from the repository root:

```bash
uv sync
uv run ruff format
uv run ruff check --fix
uv run ruff format --check
uv run ruff check
uv run pytest
```

Use targeted pytest paths while working, chosen from the actual files modified;
the runtime tests move from `tests/dev/` to `tests/tunnel/` during P1/P5. Run the
four shared component suites when component contracts change, and CLI/state/
bridge regressions when lifecycle wiring changes. Inspect formatter/linter diffs
so unrelated work is not accidentally included.

After the new lane exists, its proposed invocation is:

```bash
STLV_TEST_AWS_PROFILE=YOUR_PROFILE_NAME uv run pytest tests/integration/ --integration-tunnel -n 0 -v
```

Use the selected region/profile and P0-approved host setup. This is a future
test command, not a command supported before P6's runner changes. Other lanes
continue using their separate flags and runner-defined worker counts; never
combine tier flags. Keep the Mac awake, wait for pytest's final summary and
fixture teardown, and inspect leftover ownership after failed/interrupted runs.
Existing cleanup tooling must be extended for the new ownership/state namespace;
do not assume a name/tag sweep already knows how to reconcile session state.

Docs checks use the repository's Zensical workflow and executable example
verification described by the docs skill. Final evidence includes the concrete
implementation profile, installed-artifact checks, source/test reviews, all
A01–A14 results, and explicit cleanup outcomes. Do not publish a support claim
based on the earlier prototype's evidence.

## 9. Sequence, delivery boundaries, and completion

```text
P0 profile + proof gate
  → P1 package/policy/contracts
      → P2 AWS ownership/lifecycle
      → P3 global helper/install/uninstall
          P2 + P3 → P4 transport/DNS/recovery
              → P5 CLI/admission/full lifecycle
                  → P6 complete macOS/AWS acceptance
                      → P7 final documentation/reviews/delivery
```

Harness support and documentation preparation can accompany their owning work
packages. The arrows are dependency gates, not instructions to create a PR per
package or to delegate work. Choose reviewable changes once the baseline and
profile are established; do not split a migration so a shipped intermediate
version advertises unavailable networking.

Completion means all seven gates after G0 are satisfied, the chosen profile is
recorded, A01–A14 pass, and no cleanup/ownership/security blocker remains. If a
required live test cannot run, record that limit and leave its gate incomplete.
Implementation progress belongs in the task file. Update its index line narrowly;
do not alter another task's state or move this still-active design to history.
