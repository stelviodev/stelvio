# Linux support for Stelvio managed VPC networking

Status: proposed implementation plan, 2026-10-18. No Linux backend or acceptance
is implied by this document. Existing macOS acceptance remains separate.

## Outcome and scope

Support `stlv dev` and `stlv tunnel start` on native Linux while preserving the
current behavior: ordinary application code connects to the production resource
hostname and port, including DocumentDB TLS and member discovery. Keep explicit,
deployment-owned bastions, SSM/OpenSSH transport, transparent IPv4 TCP forwarding,
private DNS, automatic reconnection, and helper-owned cleanup.

The Linux Traforo executable should build with `CGO_ENABLED=0`. The Darwin backend
keeps its existing narrow cgo bindings. Do not require the whole project to become
cgo-free or weaken macOS protections to accommodate Linux.

Initial support contract:

- Native Linux, amd64 and arm64, with systemd as the system service manager.
- A working system bus and systemd-resolved integration actually used by normal
  host applications, with the per-link APIs described below.
- Available `/dev/net/tun`, required kernel operations, and administrator
  authorization for installation. Start with a proposed Linux 6.1+ floor; confirm
  it against the selected mechanisms before publishing a supported minimum.
- Start the CLI and forwarder as an ordinary user. Only helper administration and
  the helper process are privileged.
- Validate Fedora with SELinux enforcing and Ubuntu with its normal security
  configuration. Do not require disabling security policy or the firewall.

Defer other init systems, other resolver backends, containers, WSL, Windows,
general UDP forwarding, overlapping VPC CIDR translation, and DNS caching inside
Traforo. WSL2 needs separate network namespace, resolver, systemd, and Windows-host
acceptance; native Linux support must not implicitly advertise WSL support.

## Current code and reusable behavior

The implementation is not yet platform-independent merely because the native
calls live in `internal/darwin`:

- `pkg/traforo-client-go/internal/helper` directly imports Darwin paths, devices,
  peer identities, and filesystem operations. Its implementation files have
  Darwin build tags.
- `internal/protocol/owner.go` contains Darwin audit-token and parent receipts.
  Those receipts must not be populated with synthetic Linux values.
- `internal/darwin/bindings_darwin.go` and its C adapter provide device/route,
  process identity, ACL, loaded-image, launchd, and DNS inventory operations.
- Python `installation.py` assumes launchd, macOS authorization, and `/Library`
  paths; `assets.py` validates Mach-O images and selects Darwin-only pins.
- `.goreleaser.yml` currently applies Darwin cgo and SDK flags to its build.

Reuse the DNS relay, TCP stack/SOCKS forwarding, bounded control messages,
generation fencing, session orchestration, AWS inventory/member discovery,
invocation admission, and SSM transport where their contracts are portable.
Audit Unix-specific operations in the forwarder and Python IPC rather than
assuming that compiling them proves interoperability.

The monorepo Go source remains authoritative for the Go client API and unit
tests. The split Traforo repository owns release builds and Go implementation
CI. Implement and validate locally with `STLV_TRAFORO_DEV_BINARY`, then coordinate
a release before adding Linux download pins. Do not invent release URLs, asset
IDs, hashes, or acceptance results.

## Distribution strategy: detect capabilities

Do not choose the networking backend from `/etc/os-release`. Use that information
for diagnostics and installation instructions; choose behavior by probing the
running system.

| Distribution | Initial handling | Acceptance emphasis |
| --- | --- | --- |
| Ubuntu | Use the common resolved backend when host integration passes. | Desktop and Server networking, AppArmor where applicable, NetworkManager and networkd coexistence. |
| Fedora | Use the same backend. | SELinux enforcing, firewalld, NetworkManager reconnects. |
| Debian | Support configurations meeting the runtime contract; do not assume resolved is active. | Desktop/server variation; missing or bypassed resolved must fail before host mutation. |
| Arch | Require the same detected capabilities, independent of user-selected network manager. | Rolling versions, custom NSS and resolv.conf configuration, unsupported DNS setups. |
| Omarchy | Use capability detection and record the exact Omarchy version/configuration. | NetworkManager/resolved integration, distribution DNS changes, suspend/resume. |

NetworkManager and systemd-networkd manage connections; systemd-resolved manages
name resolution. NetworkManager can use resolved. Supporting resolved does not
require replacing NetworkManager or configuring Netplan.

Preflight must check kernel/device access, systemd and system-bus availability,
required resolved methods, current resolver domains, route/policy-rule conflicts,
and the ordinary host resolution path. Report failures with the detected
configuration and distribution-specific setup guidance. Missing capabilities
must fail before installing session routes or DNS effects.

Do not automatically install a different DNS manager, rewrite `/etc/resolv.conf`
or `/etc/nsswitch.conf`, or alter existing network connection profiles. A running
resolved daemon alone is insufficient: libc/NSS and direct resolv.conf clients
may bypass it. Explicitly document unsupported application-local DNS/DoH paths.

## Architecture and platform contracts

### Shared policy and platform adapters

Extract narrow contracts for device lifecycle, route inspection/mutation,
resolver configuration, authenticated process ownership, trusted files, and
service administration. Keep authorization, session limits, lease handling,
journal transitions, and recovery policy in shared Go logic where practical.
Keep native facts and native receipts in the appropriate backend.

Use build-tagged Darwin and Linux implementations. Avoid a single broad platform
interface or storing Unix descriptor assumptions in future Windows-neutral
protocol types. Both current backends may use descriptors internally; shared
session logic should depend on owned resources and revocable packet access.

Preserve control framing and carrier identifiers where their meaning is unchanged.
The carrier IPv4 marker is a protocol constant, not a platform `AF_INET` value.
Linux TUN should use `IFF_TUN | IFF_NO_PI`; the backend translates raw IP packets
to the existing carrier contract. Darwin's native utun header stays Darwin-only.

### Linux device and routing backend

- Open a nonpersistent TUN device in the helper with close-on-exec and bounded
  nonblocking I/O. Keep its raw descriptor out of the forwarder.
- Configure interface addresses and routes using netlink, preferably a reviewed,
  pinned Go library such as `github.com/vishvananda/netlink`.
- Inspect routes across relevant tables and policy rules. Reject unsupported
  policy routing or ambiguous ownership rather than assuming the main table
  describes actual packet delivery.
- Detect overlaps with local networks, other VPC units, and VPN routes. Preserve
  existing rejection rules and verify actual host TCP delivery before readiness.
- Preserve revocable datagram carriers and serialized helper host mutations.
- Journal durable ownership facts. An interface name, alias, or reused interface
  index alone must never authorize deletion after a crash or reboot.
- Nonpersistent TUN closure helps remove device-associated state, but cleanup
  must verify the absence of owned routes and resolver effects before succeeding.

No IP forwarding, host NAT, broad firewall exceptions, or default route changes
should be required for locally originated traffic. Test this assumption against
normal firewall and VPN configurations; diagnose blocked traffic explicitly.

### Linux process identity and trusted files

Use Unix-socket peer credentials, kernel-derived process relationships, and
pidfds where supported. Fence process identity across acquisition and checks;
do not treat a numeric PID or caller-provided parent PID as authority. Parent
death must revoke the session even if the networking child remains alive.

Define a Linux-specific durable owner receipt, including boot identity and
process start identity where needed. Do not serialize live descriptors/pidfds.
Version or tag the journal representation explicitly, retain Darwin v3 fixture
compatibility, and reject incompatible evidence without deleting it.

Validate the running privileged image against the trusted installed artifact
using Linux loaded-executable semantics, not just `os.Executable()` text. Preserve
owner, mode, ACL, symlink, hard-link, descriptor/path identity, and exclusive
publication protections. Audit Linux ACL/xattr behavior and filesystem support.

### Split DNS through systemd-resolved

For each owned VPC unit:

1. Retain the nonroot forwarder's loopback UDP/TCP DNS listener and ephemeral port.
2. Configure the owned TUN link using resolved's D-Bus `SetLinkDNSEx`, supplying
   the loopback address and listener port. Validate this combination on target
   resolved versions before treating the design as accepted.
3. Use `SetLinkDomains` with routing-only domains for resource hostnames and
   configured suffixes, and `SetLinkDefaultRoute(false)`.
4. Ensure the link's effective DNSSEC/DoT policy permits our private plaintext
   loopback relay without changing global policy. Decide whether link-specific
   overrides are necessary through tests.
5. Probe normal host `getaddrinfo()` in isolation, alongside upstream SOCKS DNS
   checks and ordinary host TCP checks. Only all required checks establish
   readiness.
6. On transport loss, retain listeners/ownership and return failure for owned
   names. Restore upstream access only for the current generation/attempt.
7. On retirement, remove owned link configuration and confirm removal. Use
   `RevertLink` only for a link whose ownership is still established.

Resolved routing domains match suffixes, including when configured with a full
resource hostname. The relay must retain exact resource-hostname enforcement;
resolver routing does not broaden relay ownership. Reject conflicting domains,
including equal-specificity competing VPN scopes that could leak private queries.

Monitor resolved restarts and loss of configuration. Reapply current owned views
and recheck readiness; mark dependent units unavailable while delivery is broken.
Account for host resolver caching when using observation tokens: use a valid,
unique owned probe name or a scoped cache strategy, rather than requiring a cached
resource lookup to reach the relay every time. Preserve the real resource lookup
and CIDR validation as separate checks.

For versions without custom-port support, initially fail with an actionable
requirement. A future dedicated per-unit loopback IP on port 53 is an alternative,
but requires additional privileged binding/address ownership and acceptance.
Do not silently funnel all DNS into Traforo or intercept DNS with firewall rules.

## Installation, privileges, and recovery

Proposed Linux locations, to finalize during implementation:

| Resource | Location |
| --- | --- |
| Installed image | `/usr/local/libexec/stelvio-traforo` |
| Durable state and replacement journal | `/var/lib/stelvio/tunnel` |
| Ephemeral socket/runtime state | `/run/stelvio/tunnel` |
| Service/socket units | `/etc/systemd/system/stelvio-traforo.{service,socket}` |

Use a system service, with socket activation if it preserves the existing lease
and shutdown contract. Authenticate clients even if ordinary users can connect.
Use narrowly elevated administration through `sudo` initially; never run the
whole `stlv` command as root or install a setuid executable.

Keep both helper and forwarder using the same verified installed image. The
forwarder refuses elevated execution. Derive an explicit systemd hardening policy
from needed operations; do not blindly enable settings such as `PrivateDevices`
that hide `/dev/net/tun`. Verify file contexts and executable placement under
SELinux. If policy changes are necessary, package/document a narrow policy rather
than disabling enforcement.

Retain the existing command contract: install/update, inspect, cleanup/reconcile,
uninstall, dev, and standalone start. Cleanup retains installation; uninstall
refuses live or unresolved sessions; upgrades use a durable replacement
transaction. Ordinary inspect/uninstall remain usable without the developer
binary or an online download.

Helper lease loss and authenticated command death initiate cleanup independently
of Python. Restart recovery reconciles only proven owned state, retries transient
failures with bounded backoff, and exposes unresolved evidence. Detect reboot and
resource identity reuse. A stale journal must never authorize removal of another
interface that acquired the same index or name.

## Assets, release builds, and developer workflow

- Split GoReleaser builds by OS: retain Darwin cgo flags and add Linux
  amd64/arm64 builds with `CGO_ENABLED=0`. Share version injection, readonly
  dependencies, trimmed paths, stripped output, and disabled VCS stamping.
- Produce proposed `traforo-linux-amd64` and `traforo-linux-arm64` assets. Validate
  ELF type/architecture, byte size, semantic version, and lack of a required
  dynamic libc/interpreter. Cross-compilation is not native acceptance.
- Extend Python selection/cache keys to `(OS, architecture)`, including explicit
  aliases such as x86_64/amd64 and aarch64/arm64. Keep existing Darwin cache and
  digest-bound legacy behavior intact.
- Extend private Session Manager Plugin downloads and executable/version checks
  for Linux with verified immutable URLs and checksums. Audit OpenSSH and all
  platform-dependent transport process flags.
- Preserve environment-only GitHub authentication, atomic caching, and safe
  developer override validation. Support Linux host builds without requiring a
  macOS SDK or Rosetta; keep Darwin developer validation unchanged.
- Keep Go source and native executables out of Python wheels and sdists. Default
  managed startup requires a real published/pinned compatible Linux release.

## Implementation phases and completion gates

### L0: Record platform contracts and preflight

Map Darwin coupling and define Linux owner/journal semantics, supported kernel
and resolved APIs, installation paths, authorization, and diagnostics. Record
exact target distribution images and versions. Prove custom-port resolved delivery
on a disposable Linux VM before committing to the DNS adapter design.

Gate: reviewed contracts and a non-AWS DNS/TUN feasibility proof; no assumptions
about distribution defaults are used as authorization for host changes.

### L1: Extract boundaries without behavior changes

Move native types/operations behind build-tagged adapters. Preserve Darwin wire
and journal fixtures and existing installation/recovery behavior.

Gate: existing Go unit/race, Python orchestration, packaging, and macOS
interoperability tests pass. No placeholder backend reports Linux as supported.

### L2: Linux helper, carriers, identity, and routes

Implement TUN, netlink, peer/process checks, trusted files, Linux receipts, lease
cleanup, and crash recovery. Add injected platform tests and real VM tests.

Gate: nonprivileged carrier interoperability and privileged routing work; process
death and helper restart restore baseline without removing foreign resources.

### L3: Resolver integration and session readiness

Implement resolved D-Bus configuration, inventory/conflicts, ownership cleanup,
restart monitoring, host resolution observation, and two-VPC tests.

Gate: unchanged resource hostnames/ports work; public DNS remains unchanged;
outages, recovery, negative answers, and cached lookups have defined behavior.

### L4: Installer, assets, and CLI integration

Implement systemd administration, safe upgrade transactions, Linux assets/plugin
selection, developer builds, and platform diagnostics. Coordinate release/pin
work only after local interoperability passes.

Gate: fresh install, repeated install, idle upgrade, active refusal, interrupted
replacement, cleanup, uninstall, and offline administration all pass. Unsupported
machines receive instructions before session host changes.

### L5: Distribution and AWS acceptance

Run the matrix below on full VMs or real machines. Containers alone cannot
certify host networking, systemd, resolver, or security-policy integration.

- Record OS image/version, architecture, kernel, systemd/resolved versions,
  NSS/resolv.conf configuration, network manager, firewall, security policy, and
  exact binary digest/source provenance.
- Test normal exit and SIGKILL of dev/standalone, orphaned child, helper crash and
  restart, reboot with stale state, DNS daemon restart, suspend/resume, network
  reconnect, and injected cleanup failure followed by `stlv tunnel cleanup`.
- Test malformed/control/ancillary input, stale generations, PID/index reuse,
  foreign resources, route conflicts, two VPCs, and concurrent administration.
- Test existing VPN interaction and policy routing, including safe refusal of
  configurations outside the initial contract.
- Run default-profile AWS proof after explicitly verifying account/region:
  public Lambda URL -> local handler -> DocumentDB TLS/member discovery/read-write.
  Exercise standalone DB access and transport reconnection too.
- Verify normal exit leaves the deployed bastion intact. Destroy proof-owned
  application infrastructure afterward, independently audit AWS resources, and
  verify host baseline restoration. Do not destroy pre-existing user deployments.

Initial acceptance order: Ubuntu amd64, Fedora amd64, Debian amd64, Arch amd64,
Omarchy amd64, then native arm64 on selected supported distributions. Record
architecture/distribution combinations individually; a successful cross-build
does not mark the remaining combinations accepted.

Go tests own native algorithms, parsers, journal fixtures, race checks, and bounded
fuzzing. Pytest owns Python behavior, asset validation, CLI ordering, environment
capture, unsupported platforms, non-VPC behavior, and genuine Python/Traforo
interoperability. Pytest must not compile Go or become a Go harness runner.

### L6: Documentation and rollout

Update README, user VPC/dev/CLI documentation, contributor instructions,
`testing.md`, both walkthroughs, and the task log together. Publish exact runtime
requirements, distro-specific setup guidance, supported/tested matrix, cleanup
instructions, and explicit unsupported configurations. Preserve historical macOS
and AWS evidence separately from new Linux results.

## Later resolver backends

If actual demand warrants it, assess NetworkManager/dnsmasq or other local
resolver integrations as independent backends. Each needs inventory, conflict
policy, scoped updates, restart handling, journal ownership, and restoration
tests. A plain resolv.conf cannot express per-domain split DNS; supporting that
configuration requires introducing/reconfiguring a local resolver or narrowing
the product promise. Do not add an invasive global fallback as incidental distro
compatibility work.

## Container support as a separate follow-up

A Linux Traforo executable could run in Docker, but initial native Linux support
does not imply container support. Ordinary application containers usually lack
systemd, a system bus, and host-integrated systemd-resolved. A cgo-free executable
removes a libc dependency; it does not supply these services or networking
privileges.

| Deployment | Required integration |
| --- | --- |
| Traforo and application in one container | TUN access, narrowly privileged helper, foreground supervision, and container DNS configuration. |
| Traforo sidecar and separate application container | The same mechanisms plus a shared network namespace or explicitly configured inter-container routing and DNS. |
| Traforo on Linux host and application in Docker | Verify container traffic reaches host-owned VPC routes through forwarding/firewall policy and that container DNS reaches the private resolver. |

### Container-local tunnel

Design the platform/service boundaries so a later container mode can start the
helper under foreground supervision instead of installing a systemd service.
Keep the existing privileged helper and ordinary-user forwarder separation; do
not run the whole application or `stlv` as root to bypass that contract. Define
trusted image/state paths and administration semantics for immutable container
images separately from host installation.

The expected rootful Docker setup requires `/dev/net/tun` device access and
`NET_ADMIN` in the relevant network namespace, typically through
`--device=/dev/net/tun` and `--cap-add=NET_ADMIN`. These are prerequisites to
validate, not a complete launch recipe. Avoid unrestricted `--privileged`, host
PID namespaces, host service sockets, and host root filesystem mounts. Rootless
Docker/user namespaces require separate feasibility and acceptance work.

Add a container DNS adapter instead of assuming systemd-resolved is present.
One candidate is a local resolver reachable on port 53 that routes owned names
to the existing per-VPC relays and forwards unrelated queries to the container's
original DNS service. This adapter must preserve Docker service-name discovery,
avoid forwarding loops, bind only in the intended namespace, handle upstream
changes, and retain the existing refusal/outage policy for private names. Resolve
privileged-port binding and multi-VPC dispatch without elevating the forwarder.
Configure the container's resolver explicitly rather than assuming the host's
split DNS is inherited.

A sidecar can share a network namespace with the application, for example using
Docker's `--network container:<name>` mechanism. Shared networking alone does
not configure each container's resolver file or share its filesystem/IPC paths.
Specify DNS, authenticated control access, process supervision, and namespace
lifetime explicitly. Avoid passing the raw TUN descriptor to the application.

Stelvio orchestration remains necessary: Traforo alone does not discover AWS
resources or establish the SSM/OpenSSH transport. Decide whether the CLI/runtime
lives in the tunnel container or communicates through a deliberately supported
control boundary. Preserve ordinary-user credential handling and process-death
cleanup across that boundary.

### Host tunnel consumed by containers

This can reuse the normal host helper, but needs explicit routing and DNS
acceptance. Bridge traffic traverses host forwarding/firewall policy, unlike the
locally originated traffic assumed by the initial Linux plan. Establish whether
return routing or NAT is needed for the chosen topology; do not add broad host
forwarding changes by default. A loopback-only host DNS relay is not directly
reachable through the container's own loopback address.

Native Linux host networking shares the host network namespace and simplifies
route access, but still requires checking the application's resolver path. Docker
Desktop on macOS and Windows places Linux containers inside a VM and needs
separate routing, DNS, device, and lifecycle acceptance. Do not equate its host
networking behavior with native Linux or imply the Darwin executable can run
inside a Linux container.

### Container acceptance gate

Keep this gate after native Linux acceptance and record supported topologies
individually:

- Prove same-container and sidecar hostname/TLS/member discovery behavior,
  including public DNS and Docker service-name resolution.
- Test multiple VPC views, transport outages, DNS failure retention, and
  reconnection without changing application connection settings.
- Kill the CLI, forwarder, helper, and sidecar independently; verify cleanup
  while other processes or namespace-sharing containers remain alive.
- Recreate containers and namespaces with stale persisted journals. Fence
  namespace identity and reused process/interface identifiers; never treat
  container-local names as proof of host ownership.
- Verify host networking/DNS remains unchanged for isolated-container tests and
  confirm all effects of deliberately shared host networking are restored.
- Exercise device/capability denial and SELinux/AppArmor restrictions, retaining
  enforcement. Track rootless and Docker Desktop acceptance separately.
- Repeat the AWS workflow and teardown audit for each topology advertised as
  supported. Container startup alone is not networking acceptance.

## Reference material

- [Linux TUN/TAP API](https://docs.kernel.org/networking/tuntap.html)
- [systemd-resolved D-Bus API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.resolve1.html)
- [NetworkManager DNS configuration](https://networkmanager.dev/docs/api/latest/NetworkManager.conf.html)
- [Ubuntu networking](https://ubuntu.com/server/docs/configuring-networks/)
- [Fedora systemd-resolved adoption](https://fedoraproject.org/wiki/Changes/systemd-resolved)
- [Omarchy networking](https://omarchy.org/manual/networking/)
- [Go netlink library](https://github.com/vishvananda/netlink)
- [Docker runtime permissions and device access](https://docs.docker.com/engine/containers/run/)
- [Docker networking and container network sharing](https://docs.docker.com/engine/network/)
- [Docker host networking](https://docs.docker.com/engine/network/drivers/host/)

These describe mechanisms and distribution context, not a guarantee of the user's
installed configuration. Recheck the APIs and exact supported distro releases
when implementation starts.
