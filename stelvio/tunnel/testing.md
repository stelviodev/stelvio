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
DNS rejection and recovery, terminal local-artifact failures, and retained helper
evidence when transport cleanup is uncertain. Readiness checks require the VPC
DNS path and resource addresses as well as a live SOCKS transport.

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
`tunnel trust reset --help`. These CLI checks do not reset trust, contact the
helper, or access AWS. Pytest does not build Go or Python artifacts.
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
