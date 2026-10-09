# Dev VPC implementation profile

Status: P0 mechanism selection complete; production implementation and release
acceptance remain P1–P7. Date: 2026-10-05.
Authority: requirements/specification and implementation plan. Live identities,
commands, failures and cleanup evidence belong in `tasks/dev-vpc.md`.

## Selected packaging and privilege boundary

Ship a compiled native C broker and a separate nonroot Go forwarding executable
as platform assets in `stelvio.tunnel`. The broker links only system libSystem;
the forwarder links libSystem/libresolv. Neither depends on a Python environment.
Production peer audit-token validation adds system libbsm to the native helper
closure. The earlier proof binary's libSystem-only inventory is historical.
Bootstrap installs the broker at a fixed root-owned location using system tools
and macOS authorization. Root never executes project Python, SSH, AWS providers,
or the TCP stack. The nonroot runtime can remain in the installed Python package.

The proof's root artifact is `dev.stelvio.vpc-proof`, separate from the future
product identity. Its inventory is one native helper, an inode-locked lease and
journal directory, one authenticated caller socket, and only session-owned utun,
routes and resolver files. Normal stop removes networking; cleanup reconciles
stale journaled files before removing the inactive installation.

Actual operation succeeded after deleting the installing venv and source copy.
Active uninstall was refused. Loaded-image vnode identity rejects a process
loaded before atomic replacement, including revalidation under installation
lock. Production install/upgrade must coordinate with that lock and use atomic
replacement; in-place rewriting is excluded. P3 implements the product installer
and protocol, including active replacement refusal and complete asset inventory.

## Selected forwarding and DNS

Use the pinned tun2socks core/gVisor stack in the nonroot Go process. Explicitly
remove/prepend Darwin's four-byte network-order AF_INET utun framing. Dispatch
connections by resolved VPC IPv4 CIDRs to one loopback SSH SOCKS endpoint per VPC.
No connection-string rewriting, hosts-file substitutions, default tunnel route,
or hand-written TCP implementation.

P3 review requires a host-ownership refinement before enabling this in the
product: unscoped route deletion cannot condition its kernel key on the checked
interface, while passing the utun FD lets a client retain the interface beyond
helper close. A per-VPC utun held solely by the helper and a revocable Unix packet
endpoint are being proved. This preserves the nonroot TCP/SSH/DNS runtime and
one atomic host lease. The actual fixed two-unit native teardown proof passed
on 2026-10-07: closing one sole-owner kernel FD removed that interface/route
while preserving the other unit, then both were absent despite a retained Unix
carrier descriptor. Temporary installed artifact was removed. A complete
packet pump and installed service still need live proof; original P0 success
does not establish those refined paths. Packet grants accept option-free IPv4
TCP only, preserving TCP options; DNS remains a separate nonroot UDP/TCP relay.

Use scoped macOS `/private/etc/resolver` files and a nonroot UDP/TCP DNS relay.
Each owned resource hostname and custom domain selects its associated VPC's
Amazon DNS resolver, queried over that VPC's SOCKS TCP path. Preserve DNS wire
responses, aliases, TTLs, negative answers and TCP fallback. Fail owned queries
closed during an outage. Resource names and member discovery must be exact;
a shared AWS regional suffix cannot be assigned to one VPC.

Two real, coherently customized VPCs passed original-hostname OS sockets, exact
1 MiB echo/half-close, real private-zone UDP/TCP and OS resolution, one-path
outage rejection, other-VPC traffic and unrelated DNS. Follow-up local proofs
passed automatic recovery in the same resolver client after about 78 seconds.
A positively controlled capture at the configured primary public resolver saw
only eight explicit control packets and zero additional packets for owned names
during resolution/outage/recovery. This proves the tested macOS resolver path,
not a general platform claim or the product supervisor's reconnect lifecycle.

## Selected AWS ownership and authentication

Use separate internal Pulumi deployments for session/VPC access units, in an
owner-specific AWSHome backend namespace with normal backend operation locks.
Never evaluate the application to destroy temporary access. Application resources
and temporary rules have separate owners. Immutable remote intent precedes
mutations; planned physical identities and atomically applied ownership tags let
SDK reconciliation recover committed creates whose returned IDs were not saved.
Persist observed cleanup IDs before deletion, validate ownership again on retry,
and confirm known-ID absence. Retain intent on uncertainty or conflict.

Ordinary independent access removal preserved both application checkpoints and
VPCs. A SDK lost-result proof killed the creator after group and rule API success
without saving returned IDs; recovery twice from a separate boto3-only venv after
removing the creator and its application creation function restored original
application identities/ingress. An actual Pulumi update was also interrupted
while group/rule resources existed. All creator/engine/provider descendants were
recorded, frozen and killed, including separately grouped plugins. A second
venv without the creation program cancelled only that dead owner's unique
backend operation, refreshed and destroyed access, preserving the application.
Do not clear pending creates blindly: reconcile exact planned identities first.
All AWS fixtures, backend versions and dedicated SSM recovery keys were removed.

The interrupted engine proof used a controlled, observed process tree. A generic
post-hoc ancestry snapshot cannot prove absence of unknown orphaned providers.
Production's supplementary guard now registers pinned native engine/provider
actors durably before an isolated Python `-I -S` launcher receives its exec ACK.
Creator death closes the sole ACK writer. Recovery freezes the creator, inventories
the explicit durable actor set, stops it and records completion before claim
transfer. Each CLI command attaches to a fresh registered AWS 7.47.0 provider;
target credentials stay distinct from Home/backend credentials. The CLI is pinned
to 3.263.0. Local preview and logical provider-only up/refresh/destroy passed across
11 commands with all actors stopped; real-process lost-return, orphan and barrier
EOF cases passed. Production access create/SSM readiness/normal disposal passed
with owner `38321ac9-f086-41ec-91ce-770956914537`. A live native `up` and its
provider were interrupted after AWS effects appeared for owner
`a4bc2999-49d7-4268-90fa-84641e3a427a`; fresh-process recovery stopped seven
registered identities, took the claim, reconciled SDK effects, refreshed/destroyed
its independent stack and removed backend versions, recovery key and metadata.
Both exact-owned fixtures were removed and target ingress preserved. Earlier
failed production runs also completed fresh-process recovery and disposal.
Local dead-provider injection after its handshake failed at the pinned engine's
RPC startup boundary; no replacement preview succeeded. Twelve native pairs
completed preview/logical up/refresh/destroy/failure checks and stopped. These
results do not close policy/CLI or actual DocumentDB acceptance. Engine execution
is bounded to 600 seconds, port startup to 10 seconds
and stream completion to 5 seconds. This does not upgrade the old controlled
tree result into a generic process-containment claim.

Use OpenSSH SOCKS forwarding over SSM, EC2 Instance Connect ephemeral client
keys, and a dedicated nonroot bastion user without sudo, PTY or session channels.
Bootstrap the Ed25519 host key over an account-bound SSM command and pin it for
OpenSSH. Both real VPC paths rejected changed host keys. No inbound SSH rule or
EC2 key pair is needed. Session transport cleanup owns every proxy descendant;
Pulumi plugins require tracking beyond the creator's process group.

Use Amazon Linux 2023 arm64, Amazon-owned AMI resolution with the exact selected
AMI recorded in access state. The tested us-east-1 image was
`ami-065b1b834d2a83a7a`; this is evidence, not a hard-coded cross-region image.
Encrypted delete-on-termination root volumes, IMDSv2, SSM-only instance roles,
public-subnet control-plane egress and dedicated forwarding user are the proven
access fixture. Production resource arguments/customization remain P2 work.

## Tested support and operational bounds

The live host was macOS 15.7.5 arm64, Python 3.12.11, Go 1.25.3 and Apple clang.
No other macOS/architecture/Python combination has live tunnel evidence. Product
support declarations and platform-asset packaging must stay within verified
coverage until additional serial acceptance lanes pass.

Pinned forwarding dependencies: tun2socks core v2.6.0 and gVisor
`v0.0.0-20250523182742-eede7a881b20`, with exact closure in the proof go.mod/go.sum.
The engine proof used Pulumi 3.263.0 and AWS provider 7.47.0.

Initial runtime budgets follow the observed mechanisms: 10-second SOCKS
handshake, 3-second DNS upstream IO, 30-second common TCP half-close deadline,
128 active forwarded connections, eight VPCs per helper lease, 90-second
forwarder startup, 60-second broker accept, 10-second ACK/host-command waits,
120-second AWS discovery/deletion confirmation and bounded three-minute resolver
recovery. A 3-second private lookup may fail while valid cached answers remain;
network readiness must be established separately. P4/P5 define observable health
and backoff behavior and test those budgets through the product coordinator.

## Gates still required

G0 establishes these implementation mechanisms. It does not establish the
DocumentDB example or `stlv dev` acceptance. P1–P7 implement and verify policy,
contracts, provisioning, native product lifecycle, supervisor/admission,
end-to-end verified TLS/discovery, package installation and documentation.


### 2026-10-07 native DNS inventory refinement (historical checkpoint)

The new SystemConfiguration/CoreFoundation inventory reads supplemental DNS
configuration through configd IPC. This expands the full native helper runtime
closure beyond the earlier detach-proof binary's libSystem/libbsm dependencies;
rebuild and verify installed-image closure before G3. A nonroot read-only native
reserved.invalid no-conflict inventory proof passed outside the sandbox. This is
configuration inventory only, not effective OS DNS or installed-artifact proof.
Resolver ownership and incomplete private journal recovery primitives are now
implemented. Unit transactions compose them, with V3 durable DNS-removal intent
and bounded read-only detach checks; service/installer/live boundary proofs remain.


### 2026-10-07 persistent namespace and first service artifact (historical checkpoint)

Production recovery state moved from volatile /private/var/run to
/Library/Application Support/Stelvio/tunnel. Its directory permits traversal
(0711) for the launchd-managed helper.sock; directory listing/writes and0600
journal/lease contents remain root-only. Parent directory entry is fsynced.
Existing shared Stelvio parent is baseline; do not remove it. Extended ACL
entries on trusted namespace/image/file/socket boundaries are refused.

The first service/client build is compiled and source/test reviewed, with a
single native mutation worker and independent main-thread packet forwarding.
Full tunnel tests444 PASS,74 warnings. Wheel/sdist contain the native asset,
manifest, sources and client; isolated-wheel validation/version/nonroot refusal
PASS. Actual installation, venv deletion/use/uninstall, live resolver generation
and packet forwarding remain NOT RUN; G3 is still incomplete. No root helper or
new AWS resources were created by this continuation.


### Final native product profile (G3 verified 2026-10-07)

The product helper is the packaged arm64 Mach-O image at the fixed trusted
`/Library/PrivilegedHelperTools/dev.stelvio.tunnel` path. Runtime closure is
system libSystem/libbsm and SystemConfiguration/CoreFoundation frameworks; no
compiler, SDK, project checkout or venv interpreter is needed by the installed
service. The activation profile remains macOS15.7.5/Darwin24.6.0 arm64. Offline
owned cleanup does not require that exact OS revision, but refuses uncertainty.

Launchd owns the fixed root0666 Unix control socket. A persistent root0711
dedicated directory contains root0600 journal/lease/admission records. A native
shared/exclusive admission flock serializes session acquisition against native
uninstall; capabilities, peer audit generations and typed snapshots remain the
session boundary. Image xattr receipts persist inode identity before exclusive
publication and fence recovery/quarantine disposal. Shared parent and foreign
settings are retained. Elevation runs a fixed system-only bootstrap and the
exact hash-checked installed native image. Packaged asset mismatches require
matching-package cleanup before installation.

Carrier v1 is a nonroot Unix datagram endpoint, with network-order unit32 and
generation64 followed by the four-byte network-order AF_INET prefix and bounded
IPv4/TCP bytes. The helper keeps every sole kernel utun descriptor. The P4
forwarder must adapt that framing to its TCP/IP stack; carrier descriptor copies
never confer or retain kernel interface ownership.

The final artifact/source fingerprints, A01/A09 and helper A10/A11 evidence,
source/test reviews, live OS DNS/TCP and crash/recovery commands and cleanup
baseline are recorded in `tasks/dev-vpc.md`. The historical NOT RUN checkpoints
above are superseded for G3. DocumentDB transport, stlv dev integration and the
complete AWS acceptance remain future gates.
