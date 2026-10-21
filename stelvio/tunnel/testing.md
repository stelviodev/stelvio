# Traforo testing

Python tests validate helper requests, reply framing, descriptor handling,
installation transactions, and forwarder control messages. They do not compile Go.
The Go module owns the forwarder and helper implementations.

## AWS transport and discovery

The transport tests use deterministic AWS SDK responses and local loopback SOCKS
peers. They verify captured credential isolation, deployed VPC and bastion
ownership, persistent first-connect SSH host identity,
DocumentDB cluster/member discovery, error classification, and cleanup of partial
SSM/SSH startup. They do not deploy AWS resources or change host routes, resolver
files, or helper state.

Run the focused suite with:

```bash
uv run pytest \
  tests/tunnel/test_aws_inventory.py \
  tests/tunnel/test_credentials.py \
  tests/tunnel/test_discovery.py \
  tests/tunnel/test_socks.py \
  tests/tunnel/test_transport.py \
  tests/tunnel/test_host_trust.py -q
```

The inventory regressions require an exact response for all declared public,
private, and isolated subnet IDs. They reject missing, duplicate, extra, changed,
or unavailable records and subnets outside the live VPC associations. Tests also
cover changing associations, secondary-block subnets, reversed API order, a
ten-AZ layout with 30 subnet records and 20 route destinations, and public-only
metadata with no route fallback. Disabled and unused VPCs request no inventory.

`VpcNetwork.cidrs` retains full AWS ownership blocks, with the verified primary
block first for resolver selection. `route_cidrs` preserves only the deployed
private and isolated subnet CIDRs. Tests keep full-VPC overlap rejection even
when the selected routes are disjoint. They use Botocore Stubber with exact
request parameters, not live AWS calls. Session wiring must use `route_cidrs` for
helper and packet configuration while keeping the owning resolver separate.

The Session Manager plugin must already be available through the verified private
cache for a live connection. Tests stub its selected path and never download or
run the plugin. A missing or unsafe plugin is a terminal local artifact error;
repair it through the existing installation command before retrying AWS transport.

The host-trust tests use isolated temporary caches. They cover independent AWS
identities, persistence, concurrent first connections, rejected key changes,
unsafe files, failed atomic publication, bounded locking, and explicit reset.
Transport tests verify that failed authentication does not publish a pin and that
new transport objects reuse it. No custom SSM Command document or Run Command
permission is required. The Session Manager plugin remains required.

Run real OpenSSH interoperability on macOS without AWS or privileged host changes:

```bash
STLV_TEST_SSH_INTEROP=1 uv run pytest tests/tunnel/test_transport.py -k actual_openssh -q
```

The test starts a temporary loopback-only sshd and a test-owned replacement for the
SSM byte stream. It checks successful authentication, persistent key reuse,
key-change rejection by a new transport, and explicit trust reset. It restores
its processes and temporary files. It does not establish live SSM acceptance.

## Shared session and isolated runtime

`test_supervisor.py` exercises independent VPC workers with deterministic
transport, helper, DNS, and discovery adapters. It covers generation advancement
after retained DNS cleanup, stable listener ports, stale control rejection,
DNS rejection and recovery, terminal local-artifact and SSH host-key failures, and retained helper
evidence when transport cleanup is uncertain. Readiness checks require the VPC
DNS path and resource addresses as well as a live SOCKS transport.

Host-key regressions cover refusal before forwarding and after an established
connection fails. They require no further retries, retained DNS rejection, and
blocked dependent invocations while unrelated endpoints remain available. Status
diagnostics are bounded and reject control characters. A fresh Python process
must reuse the persisted host pin and cannot replace it with another key.

`test_wire.py`, `test_runtime.py`, `test_dev_session.py`, and `test_evidence.py`
cover bounded IPC framing, session metadata validation, captured environment
isolation in a real child process, parent/lease death, bounded status and shutdown,
admission freshness, no-op sessions, and durable SSM ownership receipts. The
ordinary child-process tests use local fixtures only. They do not deploy AWS
resources, install the helper, or mutate host networking. The optional
`STLV_TEST_TRAFORO_IMAGE` lane is separate and exercises Python/Go interoperability
with a previously verified image.

Run the focused session suite with:

```bash
uv run pytest \
  tests/tunnel/test_wire.py \
  tests/tunnel/test_evidence.py \
  tests/tunnel/test_supervisor.py \
  tests/tunnel/test_runtime.py \
  tests/tunnel/test_dev_session.py -q
```

`test_standalone.py` checks the standalone command lifecycle. Its trust scenarios
use the real CLI, shared session, and SSH transport with deterministic AWS/SSH
boundaries. Each run creates a new session and transport. The tests verify pin
reuse, repeated changed-key refusal, actionable diagnostics, original-key
restoration, and explicit reset with confirmation. Shared fixtures live in
`tests/tunnel/conftest.py`; the separate OpenSSH test checks the real SSH behavior.

The opt-in AWS standalone scenario also compares pins across normal exit,
restart, and SIGKILL. It rotates a proof-owned bastion key, requires two fresh
commands to reject it, restores the original key, and checks database access.
Fault commands require test-only Run Command access to `AWS-RunShellScript`.
Production tunneling does not need this permission. The fault helper checks
instance ownership before each command and retains uncertainty if execution
cannot be certified; it does not race a restoration command against unknown work.

The fixture refuses pre-existing pins for proof instances. After independent AWS
absence is certified, it removes only pins claimed for the proof deployment.
Per-identity lock files remain in place. Unit tests cover pin ownership, foreign
instance refusal, command uncertainty, and cleanup ordering. Live AWS acceptance
is pending until this explicitly enabled lane is run.

`tests/test_cli_dev.py` and `tests/bridge/test_dev_admission.py` verify that `stlv dev`
captures the session before deployment cleanup, starts networking before AppSync,
closes ownership after bridge exit, and rejects expired or unavailable dependent
invocations before handler dispatch. Function tests verify that rejection happens
before importing user code. Non-VPC dispatch remains available.

The dev-command scenario in `test_standalone.py` first pins a key through
standalone mode, then runs fresh dev commands through the real shared session,
SSH transport, and bridge admission code with controlled external boundaries.
It checks that changed keys block dependent handler dispatch across restarts,
ordinary handlers still run, diagnostics include fingerprints and reset guidance,
and the original key works again in a fresh session. It does not contact AWS or
start AppSync.

Run the focused dev integration checks with:

```bash
uv run pytest \
  tests/test_cli_dev.py \
  tests/bridge/test_dev_admission.py \
  tests/aws/function/test_function_dev_mode.py \
  tests/tunnel/test_dev_session.py -q
```

## Dev mode AWS acceptance

The opt-in dev scenario uses public Function URLs to reach local handlers. Its proof
app combines `bastion=True`, `bastion={}`, a disabled-bastion VPC, and a non-VPC
Function. The DocumentDB handlers verify TLS, member discovery, and reads/writes
using distinct markers that are checked against both databases. The test stops one
bastion, requires the structured `VpcUnavailableError` before handler import,
confirms the peer and independent endpoints remain usable, then waits for both the
advanced helper generation and a fresh network-ready report before retrying. It also
rotates the proof-owned bastion SSH host key, checks terminal `ssh-host-key`
admission failure and DNS `SERVFAIL`, and restarts dev mode while the changed key
is still present. The new command must reject the same key and retain the original
pin while independent endpoints remain usable. The test restores the original key
and starts a fresh session to verify recovery. SIGINT and SIGKILL cleanup,
restart, SSM termination, retained bastions,
stable resolver ports, and unchanged deployment state are checked as well.

A separate opt-in case invokes a real non-VPC Function URL while the helper is
uninstalled. The exclusive `macos_host` fixture restores the exact helper image
captured before the run.

This lane deploys a three-VPC proof app (two bastion-enabled VPCs and one disabled
VPC) and changes host networking. It requires an authorized AWS profile and
expected account, macOS 15 or later, an installed helper, an idle host, and no
parallel test workers:

```bash
STLV_TEST_AWS_PROFILE=<authorized-profile> \
STLV_TEST_TUNNEL_AWS_ACCOUNT=<expected-account-id> \
uv run pytest \
  tests/integration/test_tunnel.py \
  --integration-macos-privileged --integration-tunnel -n 0 -q
```

The fixtures check the account before deployment. Dev and standalone acceptance
share the guarded, nonce-owned host-key rotation fixture described above. Unknown
Run Command outcomes stop further fault commands and require proof teardown;
they do not trigger a racing restoration. The non-VPC case
temporarily uninstalls the helper and lets `macos_host` restore the original bytes.
The AWS fixture destroys each proof app only after helper cleanup is certified.
These tests are separate from `tests/integration/run_all.sh` and remain pending
until explicitly authorized and run.

## Unit tests

Run the Python tunnel suite with the normal repository checks:

```bash
uv run pytest tests/tunnel -q
```

Host-mutating tests use the `integration_macos_privileged` marker. They skip in
ordinary runs and in `tests/integration/run_all.sh`. Tests that need a binary use
the checksum-verified image selected by the asset module.

## Subnet protocol and conflict diagnostics

`test_helper_protocol.py` checks 1, 8, 20, and 32 selected subnet ranges without
expanding them to a VPC block. Empty and oversized sets are rejected. A request
with 32 longest ranges and 64 longest DNS names has a 17,020-byte body, below the
32,768-byte limit. The independent shared wire fixtures remain unchanged.

`test_helper_client.py` sends actual socket replies with the fixed 26-byte route
conflict vector also checked by Go. It verifies both route labels, the interface
index, the configure stage, and uncertain status. Truncated, extended, unknown,
or wrong-status diagnostics fail closed. Received descriptors are closed on both
valid and malformed failure replies. Noncontiguous kernel masks are displayed
as addresses and masks instead of being interpreted as IPv4 host masks.

These are nonprivileged Python protocol tests. Go owns route conflict detection
and the encoder. They do not compile Go, install a helper, or change host routes.
Native routing and conflict acceptance require the separate privileged tier.

## Unprivileged forwarder smoke test

The optional smoke test starts the installed, verified Traforo image as the current
nonroot user. It uses loopback SOCKS and DNS fixtures and a datagram socket pair.
It does not contact the helper, install software, or change host routes or resolvers.

```bash
STLV_TEST_TRAFORO_FORWARDER=1 uv run pytest \
  tests/tunnel/test_forwarding.py -k verified_nonroot -q
```

Run it on macOS after `stlv tunnel install` has installed the image selected by
`STLV_TRAFORO_DEV_BINARY` or the pinned release. The installed image must match the
selected digest. The test never downloads or builds a binary.

The `test_verified_nonroot_forwarder_dns_reject_servfail_and_reconfigure` case
uses independent DNS wire queries over UDP and TCP. It verifies successful
forwarding, `SERVFAIL` while the view is rejected, no rejected query reaching the
upstream, recovery on a higher DNS revision, and a stable listener port.

## Privileged macOS acceptance

This lane can install or replace the helper and changes routes, interfaces, and
scoped resolver files while it runs. It needs macOS 15 or later, an ordinary user,
administrator authorization, and exclusive access to the host. It must run without
xdist workers and without another integration tier:

```bash
uv run pytest tests/integration/test_tunnel_host.py \
  tests/integration/test_tunnel_dns.py \
  --integration-macos-privileged -n 0 -q
```

Standalone AWS acceptance is a separate exclusive lane. It deploys a disposable
proof app with ordinary `stlv deploy`, then exercises only `stlv tunnel start`:

```bash
STLV_TEST_AWS_PROFILE=<authorized-profile> \
STLV_TEST_TUNNEL_AWS_ACCOUNT=<expected-account-id> \
uv run pytest tests/integration/test_tunnel.py \
  --integration-macos-privileged --integration-tunnel -n 0 -q
```

The fixture checks the caller account before deployment and destroys only its proof
app during teardown. Acceptance covers DocumentDB TLS, member discovery and
read/write; transport recovery while another VPC remains available; and graceful
and abrupt standalone shutdown with helper, SSM, deployment, bastion, and host
baseline checks. This lane is not part of `tests/integration/run_all.sh`. Do not run
it without explicit authorization for the AWS resources and privileged host changes.

The session fixture records the current helper and host baseline, refuses active or
uncertain state, and restores the original helper during teardown. It compares
resolver files, DNS state, routes, interfaces, and networking processes. Stop if
preflight or teardown cannot prove ownership and restoration. Do not delete retained
helper journals or resolver files by hand.

The native DNS outage case first resolves an observed test-owned name through the
ordinary macOS resolver and releases that observation before rejecting the view.
During rejection it checks direct UDP and TCP `SERVFAIL` responses from the retained
relay, a failed fresh OS lookup, the unchanged scoped resolver endpoint, and no new
upstream requests. Those checks prove the active resolver remains scoped while
failing closed; they do not claim to observe the OS query itself. Recovery uses a
fresh revision and verifies the same relay port and a successful observed OS lookup.

The host fixture accepts only the validated persistent
`/Library/PrivilegedHelperTools/.stelvio-traforo.admin.lock`. It checks that the
lock remains the same inode across administration, and treats transaction files or
unknown matching artifacts as a preflight or recovery failure.

The distinct-candidate upgrade case needs a verified candidate supplied through
`STLV_TRAFORO_DEV_BINARY`. Build and validate a local candidate without installation
or release publication:

```bash
make -C pkg/traforo-client-go build
```

The builder checks both release architectures, writes provenance, and prints the
host image path for explicit selection. The default builder command installs that
image through the existing authorized transaction. Do not use it in a build-only
lane.

Distribution acceptance uses prebuilt archive paths. Build them outside pytest:

```bash
DIST_DIR="$(mktemp -d)"
uv build --out-dir "$DIST_DIR"
STLV_TEST_STELVIO_WHEEL="$(find "$DIST_DIR" -name '*.whl' -print -quit)" \
STLV_TEST_STELVIO_SDIST="$(find "$DIST_DIR" -name '*.tar.gz' -print -quit)" \
  uv run pytest tests/tunnel/test_distribution_acceptance.py -q
```

The suite checks archive contents and installs the wheel into a clean virtual
environment outside the checkout. It verifies the installed CLI command set,
checks `tunnel status --help`, and rejects the unsupported `tunnel inspect`
command. It also checks `tunnel trust --help` and the identity selectors in
`tunnel trust reset --help`. Standalone help must show the optional environment
argument in `tunnel start --help`. These CLI checks do not start a session,
reset trust, contact the helper, or access AWS. Pytest does not build Go or Python
artifacts.
Distinct-candidate upgrade acceptance remains pending until its candidate and the
required native authorization are available.

## Native fixture subnet selection

Native DNS and lifecycle fixtures own `10.254.0.0/16` and `10.253.0.0/16`, but
route only each VPC's `.9.0/24` subnet. The helper and packet forwarder receive
the same selected destinations, including after generation changes. The test-only
packet view supplies those destinations to both client API shapes in this stack;
DNS configuration retains the ownership block to select the resolver at `.0.2`.

Resolver probes use SOCKS. Named database probes use both SOCKS and ordinary host
TCP, and the fixtures also exchange payloads through the packet carrier. Do not
add a host route for the VPC resolver to make a readiness check pass.

`tests/tunnel/test_native_network_fixture.py` checks both VPC ranges, resolver
exclusion, database inclusion, helper request encoding and forwarder controls
without root access or Go compilation. The 100-cycle lifecycle and scoped-DNS
scenarios still require the privileged lane above. Record the tested commit,
platform, binary digest, commands, skips and host restoration for each run;
nonprivileged success alone does not certify native or AWS acceptance.

## SSM termination acknowledgements

Transport cleanup validates that a successful `TerminateSession` response names
the exact owned session. Missing or mismatched responses retain ownership and do
not produce a completion receipt. An invalid-session response requires an exact
owned `Terminated` history record. Local transport shutdown and an acknowledged
termination do not assert that AWS history immediately displays `Terminated`.

Run the local response and ownership regressions with
`uv run pytest tests/tunnel/test_transport.py -q`. Live AWS acceptance is a
separate opt-in check; unit results do not certify it.

## Shared-session subnet routing and conflicts

Run the session boundary regressions without changing host networking:

```bash
uv run pytest tests/tunnel/test_forwarding.py tests/tunnel/test_supervisor.py \
  tests/tunnel/test_dev_session.py tests/tunnel/test_runtime.py -q
```

They require the same private and isolated destination ranges in helper requests
and forwarder controls. Reconnects, member refresh, and lost-reply replay keep the
selection and DNS port stable. Empty or changed selections fail before SSH, with
no whole-VPC fallback. Full VPC ownership overlap checks remain independent of
this smaller route set.

Readiness checks the resolver through SOCKS, each named resource through SOCKS
and ordinary host TCP, and system-resolver delivery using a generation-bound
observation. A successful SOCKS connection does not mask a failed direct resource
path. The resolver-only direct probe is intentionally absent: the owning VPC
resolver uses a separate SOCKS path.

Conflict tests carry decoded range and interface facts from the worker through
the bounded runtime status to the command. They reject untrusted exception text,
invalid status causes, nonprintable text, and misplaced or oversized diagnostics.
A conflict blocks dependent invocations but leaves other healthy VPCs and plain
handlers usable. Cleanup failure retains the diagnostic and ownership evidence.
Invalid-detail tests use a fixed session identifier containing `123` and check only
the reports after the invalid status. Startup identifiers must not count as leaked
diagnostics. The tests require a generic failure report and blocked VPC admission.
These tests use isolated adapters, not actual LAN or VPN mutations. Native route
conflict and host-baseline acceptance require the privileged tier in the final
stack; unit success does not certify that acceptance.


## Standalone subnet routing and diagnostics

```bash
uv run pytest tests/tunnel/test_standalone.py \
  tests/integration/test_tunnel_audit.py -q
```

These nonprivileged tests drive the real CLI capture and shared-session status
path with a controlled runtime boundary. They check two-VPC private/isolated
selection, complete ownership CIDRs, exclusion of unused components, state-context
release before startup, bounded conflict diagnostics, nonzero exit, and cleanup.
They do not substitute for the worker's host-routing tests or native acceptance.

The opt-in standalone AWS test also calls `assert_selected_routes()` after initial
readiness, transport recovery, and a fresh command start. It reads actual macOS
route lookups and requires exact private/isolated prefixes on separate per-VPC
utun interfaces. Public subnets, the primary resolver, and unused VPC space must
not use those interfaces. Each lookup is retained in
`standalone-routes.jsonl` inside the temporary proof project. Deterministic tests
reject whole-VPC routes, missing interfaces, cross-VPC interface reuse, and leaks
into each excluded address category. Live execution requires both privileged
and AWS opt-ins above; collection alone is not acceptance.


## Proof application setup and partial deployments

The standalone acceptance app uses one replica beyond the primary in its first
DocumentDB cluster and no replicas in its second cluster. Nonprivileged tests
execute the generated definition with real component constructors and controlled
resource boundaries, so configuration API changes fail before AWS acceptance.

Before destruction, acceptance discovers EC2 instances using the exact proof app
and environment tags, validates ownership, and records attached volume IDs even
when deployment outputs are incomplete. Successful empty discovery means no
volumes need capture. Failed discovery or capture remains an acceptance failure;
destruction and the absence audit still run. Proof host pins remain until all
required cleanup checks pass. Deployment interruptions also run cleanup; simultaneous
setup and cleanup failures are reported together.
The absence audit checks DocumentDB cluster and instance application/environment
ownership tags as well as readable name prefixes. This detects resources with
compacted names even after a partial deployment. Tag inspection uses resource
ARNs; missing ARNs or failed AWS requests fail the audit rather than certifying
cleanup. Both ownership tags must match before an otherwise unrelated name is
reported as belonging to the proof application.

These checks do not certify a live acceptance run.


## Dev-mode subnet routing and diagnostics

```bash
uv run pytest tests/tunnel/test_dev_session.py tests/test_cli_dev.py \
  tests/bridge/test_dev_admission.py -q
```

The combined boundary regression runs dev command capture, the actual shared
session and admission code, and actual bridge message dispatch with controlled
runtime and publish adapters. It verifies two-VPC subnet selection, preservation
of full ownership CIDRs, console conflict diagnostics, and cleanup. A conflicted
VPC is rejected before handler lookup while a healthy VPC and a plain handler
still dispatch. Malformed diagnostics block managed dependencies and are not
printed. Public invocation errors must not contain local route diagnostics.
These tests do not start an AppSync connection or mutate host networking.

The opt-in dev AWS scenarios reuse `assert_selected_routes()` after readiness,
transport recovery, process restart, and recovery from a host-key failure. They
retain the exact-route and excluded-address evidence described above, alongside
Function URL, DocumentDB TLS/member discovery/read-write, admission, and baseline
restoration checks. Collection alone does not certify live acceptance. Run the
privileged/AWS lane only with the required authorization and exclusive host
ownership. Native Intel acceptance remains pending until run on Intel hardware.


## Automated dev startup

AWS dev acceptance uses `stlv dev ENV --yes` for its proof-owned shared
environment. CLI subprocesses receive closed stdin, so an unexpected interactive
prompt fails instead of waiting for input. Production shared-environment
confirmation remains enabled when `--yes` is omitted. Deterministic tests check
the subprocess arguments, captured local-binary selection, and real CLI
confirmation behavior before the opt-in AWS run. Standalone acceptance still uses
`stlv tunnel start ENV` without deployment confirmation flags.
