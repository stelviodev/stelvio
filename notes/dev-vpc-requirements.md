# Basic VPC support for Stelvio dev mode

Requirements specification — 5 October 2026.

Status: requirements defined from the agreed scope; implementation acceptance
has not been established by this document.

## 1. Purpose and authority

Provide the smallest supported macOS implementation that lets a developer run
`stlv dev` and execute local handlers against their application's VPC resources
using ordinary resource hostnames and clients.

This document defines required behavior and release acceptance, not an
implementation plan. **MUST** and **MUST NOT** are mandatory. **MAY** identifies an
optional capability that is not required for acceptance.

The [system specification](dev-vpc-specification.md) defines the detailed
behavior, lifecycle, and logical contracts implementing these requirements.

The user decisions recorded here take precedence over the earlier
[proposal](dev-vpc-proposal.md), [plan](dev-vpc-plan.md), and prototype on
`feature/documentdb-vpc`. Those are reference material, not normative designs or
evidence that these requirements are satisfied. Earlier requirements do not
carry forward unless stated here.

## 2. Scope

Included:

- macOS development hosts.
- Automatic VPC networking through an SSH-over-SSM bastion.
- Multiple VPCs with distinct, non-overlapping CIDRs used by local handlers
  within the same dev session.
- Private IPv4 TCP connectivity and automatic resolution of supported VPC
  resources' hostnames, including DocumentDB-discovered member hostnames.
- Custom DNS domains declared through `BastionConfig` or dictionary input,
  resolved using the associated VPC's DNS view.
- A globally installed local helper usable by different projects and virtual
  environments, with projects used sequentially.
- Automatic reconnection, session shutdown, temporary AWS-resource removal,
  and local helper installation and uninstallation.
- A real integration test of the VPC tunnel example.

Deferred:

- Linux and Windows VPC tunneling; `--network managed` and `--network external`.
- Concurrent VPC dev sessions on one development host.
- Overlapping VPC CIDRs within a dev session.
- Guaranteed compatibility between different Stelvio versions sharing a helper.
- Automatic provisioning of custom private hosted zones, arbitrary
  private-service inventory, and a generic discovery plugin API.
- General UDP, ICMP, IPv6 tunneling, and interception of arbitrary custom DNS
  clients.
- NAT-instance reuse, existing bastion adoption, or additional tunnel backends.
- General invocation-bridge redesign, new handler concurrency, and forced
  cancellation of Python handlers.

The supported macOS releases and CPU architectures MUST be documented and backed
by acceptance evidence before release. This document does not infer that every
macOS version or architecture is supported.

## 3. User-facing behavior

### R01 — Default dev workflow

After the one-time `stlv tunnel install` and documented local/AWS prerequisites,
running **`stlv dev` with no additional parameters** from
`spikes/vpc-tunnel-app` MUST start working VPC development networking. The user
MUST NOT need to start a tunnel separately, supply resource hostnames manually,
change connection strings, or run the dev process as root.

`stlv dev --network auto` MUST have identical networking behavior. `auto` is the
only supported network mode in this release. `managed`, `external`, and unknown
values MUST fail with a clear unsupported-value diagnostic before deployment or
host networking changes. There MUST NOT be a silent external-network fallback.

An application without VPC requirements MUST retain its existing dev behavior
without requiring the tunnel helper or creating a bastion.

### R02 — VPC bastion configuration

`bastion=` MUST remain a `Vpc` argument. Omission MUST mean `bastion=None`.

| Configuration | `stlv deploy` | `stlv dev` | On dev exit |
| --- | --- | --- | --- |
| Omitted or `None` | No bastion | Automatically create a session-owned bastion when VPC networking is needed | Remove the session-owned bastion and its session-owned supporting resources |
| `True` | Provision an explicitly configured bastion | Use the explicitly configured bastion, provisioning it if necessary | Retain the explicitly configured bastion |
| `BastionConfig(...)` or dictionary | Same persistent-bastion policy as `True`; retain declared DNS configuration | Same as `True`, with custom `dns_domains` resolved through this VPC | Retain the explicitly configured bastion; remove session-owned local DNS state |
| `False` | Create no bastion and emit a warning | Warn that VPC resources are inaccessible; start the rest of dev mode without a bastion or tunnel for this VPC | No bastion to remove for this VPC |

Configuration-object and dictionary forms MUST remain supported. An empty
configuration enables a persistent bastion; it MUST NOT be treated as `False`.
Non-empty `dns_domains` MUST be supported, not silently ignored or rejected
merely because custom DNS was deferred in the earlier scope.

The warning for `bastion=False` during ordinary deploy MUST distinguish an
explicit `False` from omission/`None`; omission MUST NOT trigger that warning.

During dev, explicit `False` MUST NOT be overridden by automatic provisioning
and MUST NOT block startup of the rest of the dev server. The warning MUST name
the affected VPC and explain that Stelvio is not providing access to its
resources. This is an explicit opt-out, not an external-network mode or a claim
that independently supplied connectivity has been verified. Handlers may still
run; resource connections may fail. The readiness requirements below MUST NOT
turn this intentional opt-out into a global startup failure.

Automatic provisioning MUST NOT require changing the application's VPC
declaration. The existing example with `bastion=True` MUST work; the same example
with that argument omitted MUST also work and exercise the temporary lifecycle.

### R03 — Temporary AWS infrastructure ownership

For `bastion=None`, a normal exit, Ctrl+C, or handled startup/runtime failure MUST
remove the temporary bastion and all AWS resources created solely
for that session's access, including temporary access rules. Removal MUST be
reflected in whichever infrastructure state owns those resources.

Shutdown MUST NOT destroy the application's VPC, database, functions, NAT
resources, or an explicitly configured persistent bastion. Teardown MUST NOT
switch the application's remaining resources from dev to ordinary deployment
behavior as a side effect.

A process kill, host crash, unavailable AWS API, or revoked credentials can
prevent immediate AWS deletion. Ownership MUST be durably recorded so leftovers
can be identified and removed through a documented recovery procedure. Failed
deletion MUST NOT be reported as success. When the CLI can still report, it MUST
identify the remaining resources and recovery action. A later session MUST NOT
silently lose ownership of those resources.

### R04 — Global helper installation

`stlv tunnel install` MUST install a shared macOS helper, including the runtime
dependencies it needs, independently of the invoking project's venv. It MUST be
usable from at least two separate projects with separate venvs using the same
supported Stelvio version.

Deactivating, moving, or deleting the installing project's venv MUST NOT break
the helper for another compatible project. Privileged execution MUST NOT import
code or load a Python runtime from a user-writable project or venv.

Installation MUST identify its system changes and request macOS elevation where
needed. Repeated installation of a compatible version MUST be safe and MUST NOT
create duplicate helper installations or privileges. Partial installation
failure MUST leave a recoverable, accurately reported state. Installation MUST
NOT deploy AWS resources.

The CLI MUST detect a missing or incompatible helper and give a concrete remedy
before changing host networking. Cross-version compatibility MAY be supported;
silently running an incompatible helper MUST NOT be supported.

### R05 — Cleanup and uninstallation

`stlv tunnel cleanup` MUST remove stale Stelvio-owned local networking state,
stop the installed helper when safe, and uninstall the helper and its dedicated
files, service registrations, and privilege grants. It MUST work from a
compatible project's venv even if another project's venv installed the helper.

Repeated cleanup, including when nothing is installed, MUST be safe. Cleanup
MUST NOT remove unrelated routing, firewall, DNS, services, files, or privileges.
Failures MUST identify what remains; the command MUST NOT report successful
uninstallation while required cleanup remains incomplete.

If a dev session is active, cleanup MUST refuse before changing session state
or uninstalling anything, explain that the session must be stopped first, and
return a failure status. It MUST NOT terminate that session's tunnel. Session
ownership checks MUST distinguish an active session from stale crash remnants.

This command manages the local installation. Temporary AWS-resource deletion is
the dev lifecycle's responsibility under R03, not an implicit AWS destroy
operation performed by helper uninstallation.

## 4. Connectivity and session behavior

### R06 — Transparent resource access and discovery

Local handlers MUST connect through ordinary clients using the linked resource's
hostnames, ports, credentials, and connection options. In particular, DocumentDB
MUST work with TLS certificate and hostname verification enabled and its normal
replica-set discovery enabled. Localhost substitution, disabled verification,
or disabling discovery MUST NOT be used to satisfy acceptance.

Stelvio MUST automatically derive the hostnames and network destinations needed
by supported VPC resources. Discovery MUST cover endpoints learned by the
database driver, not only the initial cluster endpoint. The example MUST need
no manual DNS suffix, member-host list, or workstation hosts-file entry.

Custom `dns_domains` MUST resolve through their associated VPC's DNS view,
including names in an existing private hosted zone associated with that VPC.
The feature does not create those hosted zones or replace the application's
responsibility for its DNS records and resource access rules. Ambiguous domain
ownership across VPCs MUST be rejected with a clear diagnostic.

Only required VPC traffic and resource or declared-domain DNS handling may be
changed. Unrelated workstation traffic and name resolution, including the AWS control-plane connection used to
establish the tunnel, MUST retain their existing behavior. Support for custom
private-zone resolution MUST be included in acceptance; automatic zone
provisioning is not required.

### R07 — AWS access

The bastion MUST reach the supported resource's actual service port, including
resources in isolated subnets. Stelvio MUST provision the access needed for its
managed resources without opening them to the public internet or broadening
unrelated workload access rules. Temporary access rules MUST follow R03.

The existing AWS invocation bridge MUST continue to deliver an event to the
local handler and return its result. The dev stub MUST remain able to reach the
bridge without depending on application VPC egress. Local AWS API calls MUST
continue to use the configured developer credentials.

This feature supplies network reachability; it does not reproduce a deployed
Lambda's IAM identity or enforce its per-function security-group isolation.

### R08 — Startup and readiness

Dev mode MUST derive networking requirements from the application and its
resolved resources. It MUST NOT require the user to maintain a separate network
manifest.

Startup MUST establish and verify the required network path before admitting
affected local handler invocations, except for the explicit opt-out in R02.
A running helper process or open local
listener alone MUST NOT count as readiness. Startup MUST have bounded waits and
actionable failure reporting for missing dependencies, authentication failures,
bastion readiness, DNS failures, and unreachable resources.

The CLI MUST visibly distinguish starting, ready, reconnecting, failed, and
stopping. Known failures MUST NOT be concealed behind a ready status.

### R09 — Session lifetime and automatic reconnection

Networking MUST remain available across handler reloads, idle time, and handler
execution. Per-invocation environment changes MUST NOT change the tunnel's AWS
identity or configuration.

Transport failure MUST be detected and cause the session to leave ready state.
For recoverable failures, Stelvio MUST automatically retry with backoff and
re-establish authenticated connectivity without restarting `stlv dev`. New
affected invocations MUST NOT execute against a network known to be unavailable;
they MUST receive an actionable failure within their invocation budget.

Recovery MUST revalidate connectivity before declaring readiness. Existing
sockets may fail; successful reconnection guarantees that new connections can
work, not that broken sockets are restored. Stelvio MUST NOT automatically
replay handler invocations or database operations to hide the interruption.

Authentication or configuration failures requiring user action MUST be reported
as such. Retrying MUST NOT disable authentication checks, spin without backoff,
or prevent Ctrl+C and cleanup.

If an individual enabled VPC fails to start or later requires user intervention,
the dev server MUST keep running. Only invocations depending on that failed VPC
are rejected; healthy-VPC and non-VPC handlers remain available. The failed
VPC's temporary resources, transport, and local forwarding MUST be cleaned
independently, with its failure and any unfinished cleanup remaining visible.
While the session continues, retain only the owned DNS rejection configuration
needed to prevent private-domain queries from falling back to public resolution;
remove it at session exit.

### R10 — Local lifecycle and conflicts

Normal shutdown and handled global startup failures MUST remove session-owned
local network changes and terminate session-owned transport processes. A failed
VPC within a continuing session follows R09. The global
helper installation MUST remain for reuse until explicit `stlv tunnel cleanup`.

After abnormal termination, stale local state MUST be identifiable and safely
recoverable by a subsequent start or cleanup. Ownership checks MUST prevent
cleanup from removing another process's or application's resources.

Only one dev session using the helper per host is required. A second active
session requiring the helper MUST be rejected clearly without disrupting the
first. Sequential project switching
MUST NOT retain the previous project's routes, DNS configuration, credentials,
or session configuration.

Conflicting network configuration MUST be reported before conflicting host
changes are installed. The implementation MUST NOT silently take over an
unrelated VPN or network configuration.

### R11 — Multiple VPCs within a session

A single dev session MUST support local handlers accessing resources across
multiple VPCs. It MUST NOT select only one VPC or require separate CLI sessions
for each. Bastion configuration and resource ownership MUST apply independently
to each VPC, including a mixture of temporary bastions, explicitly configured
persistent bastions, and disabled access.

Automatic discovery, resource access, verified transport, reconnection, and
cleanup MUST work for every enabled VPC. The CLI MUST identify which VPC a
readiness or connection failure concerns; a failure MUST NOT be reported as
successful connectivity for that VPC. Shared helper state MUST preserve the
other VPCs' network configuration when one VPC reconnects or is cleaned up.

The VPCs requiring tunnel access MUST have distinct, non-overlapping CIDRs.
Overlapping ranges MUST cause an actionable startup error identifying the VPCs
and conflicting ranges before conflicting local networking changes are applied.
The implementation MUST NOT select one of the overlapping VPCs arbitrarily.
Supporting overlapping ranges is deferred.

## 5. Security and code organization

### R12 — Authentication and privilege

- The connection MUST use SSH over AWS Systems Manager Session Manager.
- The bastion MUST require no public inbound SSH access.
- SSH server identity MUST be verified on initial connection and reconnection.
  Unknown or changed identity MUST NOT be accepted by disabling verification.
- The CLI, application handlers, AWS credential handling, and network transport
  MUST run without local root privilege. Local elevation MUST be limited to
  installing/removing the trusted helper and the OS operations that require it.
- The helper MUST validate callers, session ownership, and requested network
  changes. It MUST NOT offer arbitrary privileged commands, executable paths,
  or loading of code from a project/venv.
- Secret values MUST NOT appear in ordinary logs, command arguments, or
  non-secret metadata. Temporary authentication material MUST be protected and
  cleaned up with its owning session.

### R13 — Module location

The networking runtime and helper implementation MUST live under
`stelvio.tunnel`, replacing `stelvio.dev` as their implementation package. CLI
commands and AWS component integration may remain in their existing respective
packages and call into `stelvio.tunnel`.

This requirement does not prescribe the internal module split, helper runtime,
IPC mechanism, TCP forwarding mechanism, bastion size, or AWS resource ownership
mechanism. Those choices must satisfy the behavior and security above.

## 6. Acceptance criteria

Tests MUST exercise observable behavior. Unit tests and mocked infrastructure
tests supplement, but do not replace, real macOS/AWS acceptance.

| ID | Required demonstration | Requirements |
| --- | --- | --- |
| A01 | Install once from project A's venv; run a VPC dev session, stop it, and run project B from a separate venv with the same Stelvio version. Remove or move A's venv before running B. No reinstall or dependency on A's paths is needed. | R04, R10, R12 |
| A02 | Launch the actual `stlv dev` CLI with no additional parameters against the example and invoke its deployed Function URL. Prove execution occurred locally; assert a successful database ping, write/read-back, and discovery of real DocumentDB hosts with TLS and hostname verification enabled. | R01, R06–R08 |
| A03 | Repeat the example with `bastion` omitted. Verify automatic bastion/access creation and removal on normal exit and Ctrl+C; retain the application resources. Verify ordinary deploy with omission creates no bastion. | R02, R03 |
| A04 | Exercise the full bastion behavior table, including `BastionConfig`/dictionary forms and empty configurations, both explicit `False` warnings, continued dev-server operation with `False`, absence of a bastion for `False`, and persistence of explicit bastions after dev exits. | R02 |
| A05 | Confirm explicit `--network auto` has the same behavior as the default; reject deferred and invalid modes. Run a non-VPC dev application without the helper. | R01 |
| A06 | Interrupt a running tunnel, observe loss of readiness, restore connectivity, and verify automatic recovery and a successful new database invocation without restarting dev. Confirm no application invocation is replayed. | R08, R09 |
| A07 | Reload a handler, leave the session idle, and run handler activity while verifying that networking remains usable and retains the original AWS configuration. | R09 |
| A08 | Exercise failed startup, transport failure, normal exit, and abrupt parent termination. Verify local recovery, temporary AWS-resource ownership/removal or explicit recoverable leftovers, and preservation of unrelated settings and application resources. | R03, R10 |
| A09 | First run cleanup during an active dev session and verify refusal without disruption. After stopping dev, run cleanup from project B after installation from A. Verify stale local state, helper files, services, and privilege grants are removed; run it again successfully. Reinstall and run a fresh session successfully. | R04, R05 |
| A10 | Reject a second active host session, incompatible helper, and conflicting host network configuration without disturbing the active session or unrelated settings. | R04, R10 |
| A11 | Verify SSH-over-SSM operation, absence of public inbound SSH, rejection of untrusted server identity, non-root handler/transport execution, and rejection of arbitrary privileged helper requests. | R12 |
| A12 | Verify unrelated DNS/network traffic continues to work; resource cluster and discovered-member names work without manual configuration; custom `dns_domains` resolve existing private-zone records through the owning VPC, fail appropriately during outage, and leave no local resolver state after exit; ambiguous domain ownership is rejected; missing prerequisites produce actionable diagnostics. | R04, R06, R08 |
| A13 | Verify package imports, installation artifacts, and runtime entry points use `stelvio.tunnel` for the networking implementation. | R13 |
| A14 | In one actual dev session, invoke local handlers that access resources in at least two VPCs with non-overlapping CIDRs. Verify discovery and connectivity to both, reconnection for each, and cleanup of both paths. Fail one VPC at startup and at runtime; verify rejection of only its dependent invocations while healthy-VPC and non-VPC handlers continue. Exercise mixed bastion settings: retain an explicit bastion, remove a temporary one, and leave an opted-out VPC without managed access while the rest of dev works. Separately verify rejection of overlapping CIDRs without conflicting local changes. | R02, R03, R06–R11 |

### Required end-to-end integration test

A02 MUST be implemented as a repeatable integration test, not only a manual test
recipe. It MUST use the example's application and handler, or an explicitly
synchronized fixture that preserves their relevant behavior. Any test-only local
execution marker MUST NOT change the database connection path.

The test MUST run on an actual supported macOS host, deploy real AWS resources,
start the actual CLI and installed helper, invoke the AWS entry point, and
exercise the local handler's real database client through the tunnel. Starting
only a bridge listener or deploying with `dev_mode=True` is insufficient.

The suite MUST also test the omitted-bastion variant, automatic reconnection,
and simultaneous access to distinct VPCs within one dev session.
Fixtures MUST clean up their AWS and local resources even on assertion failure,
and identify leftovers when cleanup cannot complete. Prerequisites and the test
command MUST be documented so the test can be rerun.

A skipped test or mock-only pass MUST NOT count as release acceptance. Record
the tested macOS version/architecture, Python and Stelvio/helper versions, AWS
region, relevant dependency versions, and results. No live verification is
claimed by this requirements document.
