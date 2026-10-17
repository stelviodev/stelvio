# Traforo v2 reviewer and implementation handover

Working branch: `feature/vpc-traforo-v2`. Do not commit or switch branches unless requested.
This file and the self-contained HTML were copied from `feature/documentdb-vpc-v1`
and updated for persistent-only Traforo. Current source wins over dated snippets.
Read `tasks/dev-vpc.md` first for current validation and exact next steps.

## Contract and decisions

- `Vpc(bastion=False)` is the default; explicit `None` is invalid. True, BastionConfig
  and dictionaries (including {}) create persistent app-owned access through deploy.
- No temporary provisioner, independent Pulumi access stack/backend, AWS recovery
  keys/journals, creator-tree fencing, or `tunnel recover`. Connection recovery and
  owned host cleanup journals remain necessary and are separate concepts.
- Disabled handler dependencies warn and run without managed networking/admission.
  Production VPC attachment validation remains intact.
- `dev` and foreground `tunnel start [ENV]` share NetworkSession. Start reads deployed
  state and uses handler dependencies only; it does not deploy or run the bridge.
  No `--network-mode`. No eligible dependencies means no helper/plugin activation.
- Plugin is pinned privately alongside Pulumi: versioned AWS URLs and archive/binary
  digests in plugin.py, atomic publication and lock, exact-path invocation. No PATH
  fallback, installer-script execution, or AWS executable in Git/wheel.
- `tunnel cleanup` reconciles stale host effects and retains installation;
  `reconcile` is an alias. `uninstall` removes helper, retains plugin cache.
- Install/first use replaces an idle older helper using its trusted installed image
  for safe retirement and verified staged new image for publication. Legacy helper
  uses its own receipt-validating --uninstall; old Python package is unnecessary.
  Active/uncertain/foreign state must never be overwritten.

## Architecture and source map

Invocation: Function URL → Lambda stub → AppSync Events WSS → local bridge/handler
→ correlated result back. Database bytes never travel on this WebSocket.

Networking: isolated nonroot Python runtime owns captured AWS context, per-VPC
supervisors, discovery and DNS relay. Local socket → route/utun → root helper →
revocable Unix datagram carrier → nonroot Traforo forwarder (tun2socks/gVisor)
→ OpenSSH SOCKS → private plugin SSM WSS → SSM agent → loopback forwarding-only
sshd → private service. Database TLS remains end-to-end. DNS: system resolver →
scoped /etc/resolver entry → Python relay → SOCKS/SSH TCP → VPC resolver (+2).

One downloaded Go+cgo executable has separate helper and forwarder processes. Root
retains raw utun descriptors and owns journaled host mutations. Peer audit token
and birth identity authenticate the runtime. Snapshot v4 additionally persists
its kernel-derived command parent birth identity, so an orphaned live/frozen
runtime cannot keep host authority after command death. v3 journals remain
readable; ownership is never inferred solely from a PID or saved kernel index.

Start with CLI in stelvio/cli/tunnel.py and cli/commands.py, capture in command_run.py,
policy/metadata in aws/vpc/vpc.py and tunnel/manifest.py, shared session in
`tunnel/session.py`, isolated runtime in runtime.py/runtime_child.py, transport in
transport.py/ssm_proxy.py, and host administration in installation.py.
The canonical Go source and release CI now live in
`https://github.com/stelviodev/traforo`. `pkg/traforo-client-go` remains a reference
snapshot only, excluded from wheel/sdist. There is no local release builder.
`assets.py` pins v0.0.4 URLs, GitHub asset IDs and architecture SHA-256 values.
`ensure_traforo()` locks, validates and atomically caches a download under
`get_bin_path()`. `GITHUB_TOKEN`, when provided by the environment, authenticates
only the GitHub API request; cross-origin redirects drop authorization. Otherwise
use the pinned public download URL. Production never calls `gh`. Both helper and
forwarder still execute the same root-owned installed image; forwarder checks its
pin without a cache or network operation. Normal uninstall also works offline.


## Answering a reviewer

The HTML has eight chapters: Architecture, Trace, Components, Principles,
Comparison, Code, Alternatives, Evidence. Admission selectors demonstrate
readiness versus disabled bypass. The trace stepper distinguishes deploy/dev/start.
Component columns explain necessity, removal loss, and SST equivalent. Requirement
cards defend each adapter; alternatives toggles show when fixed forwarding is
simpler. Source excerpts have full-file hashes; refresh them after edits.

SST comparison is pinned to a0bd20f762883e72a35caccb4896c42ce5b3f707 in the local
checkout `/Users/sebst/Code/anomalyco/sst`. Its tunnel uses direct public Go SSH,
Go SOCKS on 127.0.0.1:1080, tun2socks, fixed utun69, route/ifconfig and sudoers.
The proxy uses InsecureIgnoreHostKey. Do not generalize this snapshot to current
SST or claim equivalent scoped DNS/per-VPC readiness without finding that code.
SSH-over-SSM corresponds to the transport segment, not host routing/DNS/ownership.

Root access is required for the chosen macOS host routing/resolver integration,
not by the abstract requirement of reaching one DB. A fixed forward, proxy-aware
client or externally managed VPN can shift/remove those requirements. Discuss
TLS hostname preservation and replica member discovery before claiming a local
port-forward is an equivalent replacement.

## Validation and continuation

Current v2 native arm64 routing/DNS, idle/interrupted/legacy upgrades, owner
death with a frozen child, helper restart and failed-cleanup recovery passed on
macOS 15.7.5 / Go 1.27.2. Go race/fuzz, Rosetta package tests, 510 Python
regressions and clean installed-wheel checks passed. The actual default-profile
AWS proof passed dev/standalone TLS/member/read-write/reconnect, retained the
bastion after normal stop, standalone SIGKILL cleanup, and independent final
application teardown/audits (61 backend versions, one log group and passphrase
removed). Final public-command uninstall removed the helper/plist/state and
replacement artifacts; exact interfaces/resolver digests match baseline, and no
IPv4 utun routes remain. The private plugin cache is intentionally retained.
Extra config/multi-VPC AWS cases and a separate full dev SIGKILL run
remain unverified for v2. See
tasks/dev-vpc.md for the final status. Earlier acceptance is historical. Native Intel networking and Linux/WSL/Windows backends
remain out of scope. Release pins must be updated deliberately after upstream CI produces compatible
artifacts. Do not build the reference copy or claim it exactly matches the pinned
release. Historical native/Go results above predate this distribution change.

Go tests belong upstream. Python release/download tests cover authenticated and
anonymous requests, redirects, bounded corrupt downloads, cache concurrency and
atomic publication. Frozen wire vectors remain independently readable by Python.
See `testing.md` for the revised installed-wheel/native/AWS runbook. Enable actual
cached-binary interop by setting `STLV_TEST_TRAFORO_IMAGE` to the verified cached
executable path; default unit tests stay offline. Wheel/sdist must contain neither native executables nor reference Go code.
AWS lane must prove actual example TLS/member discovery/read-write, standalone
access, transport recovery and persistent bastion retention; destroy fixture
application resources and independently audit AWS absence and exact host baseline.
Native lifecycle must include SIGKILL, orphaned runtime, restart, failed cleanup,
active upgrade/uninstall refusal and interrupted upgrade resumption.

Keep implementation, tests, public docs and both walkthroughs synchronized. Append
new evidence to the task log; never recast old failures or earlier builds as v2
acceptance. No root or AWS changes should be inferred from documentation alone.


## Downloaded-release acceptance (2026-10-17)

Pinned external v0.0.4 is validated separately from the historical local builds.
Both SHA pins and native/Rosetta version/library checks pass. Upstream CI and release
build succeeded at 01603648943d5278f6b727eec0a49f9ba33e8df4. Final Python regressions:
455 pass, including explicit downloaded-image process interop. Wheel/sdist contain
neither executables nor the retained reference Go source.

Native arm64 macOS 15.7.5 two-unit TCP/DNS, ownership/descriptor/generation checks,
frozen orphan death, helper restart, failed-cleanup recovery, idle/interrupted/legacy
upgrades, fresh install and offline uninstall pass. Default-profile/us-east-1 real
AWS app stlv-657643/test passed dev+standalone TLS/member/read-write/reconnection and
standalone SIGKILL in 1541.12 seconds including teardown. Normal exit retained the
bastion. AWS teardown and independent absence audits pass (62 backend versions,
one log group and passphrase removed). The pre-existing idle helper was restored;
exact interface/resolver baseline restored, route changes only LAN ARP expiry timers.
Caches deliberately remain. Evidence: /private/tmp/traforo-release-proof and task log.
Native Intel/other macOS and additional AWS config/multi-VPC/dev-SIGKILL cases remain
uncertified by this run. No CI changes, commits, staging or branch switches.
