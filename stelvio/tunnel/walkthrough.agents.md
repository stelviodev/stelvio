# Traforo reviewer and implementation guide

Use this guide with `walkthrough.html` for architecture review and implementation
changes. Current source is authoritative. The HTML contains self-contained source
excerpts; refresh their line ranges and SHA-256 fingerprints after source edits.

## Contract

- `Vpc(bastion=False)` is the default. `True`, `BastionConfig`, and dictionaries
  (including `{}`) request persistent application-owned access. Explicit `None`
  is invalid. Deployment creates and removes bastions and their supporting resources.
- A handler dependency without a configured bastion warns and runs without managed
  networking. Production validation still requires the Function's correct VPC attachment.
- `stlv dev` and foreground `stlv tunnel start [ENV]` share `NetworkSession`.
  Standalone start reads existing deployed state and selects handler dependencies;
  it does not deploy or start the invocation bridge. No eligible dependencies means
  no helper or plugin activation.
- A configured but unavailable bastion fails dependent invocation readiness.
  Reconnection never provisions or replaces infrastructure.
- `tunnel cleanup` reconciles stale owned host effects and keeps the installation.
  `tunnel uninstall` removes the helper after safe cleanup and retains the plugin cache.
- Installation replaces an idle installed image through a staged privileged transaction.
  Active sessions, foreign resources, and uncertain ownership must never be overwritten.

## Architecture and source map

Invocation: Function URL → Lambda stub → AppSync Events WSS → local bridge → local
handler → correlated response. Database traffic uses a separate path.

Networking: handler socket → macOS route/utun → root helper → revocable Unix datagram
carrier → nonroot Traforo forwarder (tun2socks/gVisor) → OpenSSH SOCKS → Session
Manager plugin SSM WSS → SSM agent → loopback forwarding-only sshd → private service.
Database TLS remains end-to-end. DNS: system resolver → scoped `/etc/resolver`
entry → Traforo loopback UDP/TCP relay → SOCKS/SSH TCP → VPC resolver (+2).

One Go+cgo executable runs as two processes. The helper retains raw utun descriptors,
serializes host mutations, and journals ownership. Audit tokens and birth identities
authenticate the runtime. The durable snapshot includes the command-parent identity
so a frozen or surviving runtime cannot retain host authority after command death.
Ownership is never inferred solely from a PID or a saved kernel interface index.

Key implementation entry points:

| Concern | Source |
| --- | --- |
| Commands and environment resolution | `stelvio/cli/tunnel.py`, `stelvio/cli/commands.py`, `stelvio/command_run.py` |
| Deployment policy and metadata | `stelvio/aws/vpc/vpc.py`, `stelvio/tunnel/manifest.py`, `stelvio/stack_outputs.py` |
| Shared session and isolated process | `stelvio/tunnel/session.py`, `runtime.py`, `runtime_child.py` |
| Discovery, readiness and transport | `discovery.py`, `supervisor.py`, `transport.py`, `ssm_proxy.py` |
| Image selection and administration | `assets.py`, `installation.py`, `plugin.py` |
| Helper IPC and forwarder control | `helper_protocol.py`, `helper_client.py`, `forwarding.py` |
| Go implementation | `pkg/traforo-client-go/internal/{helper,darwin,forwarder,dnsrelay,protocol}` |

## Release selection and local development

The monorepo Go source is authoritative for its client API and unit tests. The
repository split mirrors it to <https://github.com/stelviodev/traforo>, which owns
release builds and Go implementation CI. Go source, developer scripts, and native
executables are excluded from the Python wheel and sdist.

`assets.py` pins v0.0.7 URLs, GitHub asset IDs, and per-architecture SHA-256 digests.
Downloads are locked, validated, and atomically cached under `get_bin_path()`.
Authentication uses only an environment-supplied `GITHUB_TOKEN`; cross-origin
redirects drop authorization. Production does not invoke `gh`. The Session Manager
plugin has a separate verified private installer; transport always uses its exact path.

Release images must report the exact pinned semantic version. A snapshot of the
same base version is accepted only through `STLV_TRAFORO_DEV_BINARY`. Validate the
selected image before authorization or host changes. The forwarder executes the
verified root-owned installed image and checks its digest/version without downloading.
Inspect, cleanup, and ordinary uninstall work without a developer build file.

```bash
cd pkg/traforo-client-go
make lint vet test race
cd ../..
uv run python pkg/traforo-client-go/scripts/build.py
# Run the exact export command printed by the builder in every networking shell.
uv run stlv dev
```

The builder and release workflow use the same GoReleaser configuration and tool
version. Local snapshots use the pinned base plus commit identity; provenance
records the source/config fingerprint. Build failure preserves published local
outputs. `make build` uses the build-only path and does not install the helper.
To return to the release, unset the override and run `stlv tunnel install`.

## DNS and readiness

Go owns listener pairs, DNS parsing, upstream TCP exchanges, resource address
validation, and probes. Python owns AWS discovery, exact hostname/suffix ownership,
member refresh, reconnection, and invocation admission. The helper installs the
scoped resolver entries after Go returns a listener port.

Listeners outlive transport attempts. During an outage, owned queries receive
SERVFAIL; unowned names receive REFUSED. Unit, host generation, transport attempt,
and DNS revision fence changes and responses. There is no relay cache. Request
limits, I/O deadlines, and bounded shutdown apply to DNS workers and sockets.

Readiness needs both upstream SOCKS probes and direct host TCP through utun.
Python's isolated `getaddrinfo()` process additionally proves macOS scoped delivery
using generation-bound Go observation tokens. A successful SOCKS probe alone is
insufficient. Preserve this distinction when editing probes or health checks.

## Guiding a human reviewer

The HTML chapters cover architecture, request trace, components, requirements,
comparison, code, alternatives, Go DNS, regression tests, and validation gates.
Admission selectors demonstrate per-handler readiness. Component columns explain
necessity, the loss on removal, and SST equivalents.

The SST comparison is pinned to revision
`a0bd20f762883e72a35caccb4896c42ce5b3f707`, using source excerpts recorded in the HTML.
It shows direct public Go SSH, Go SOCKS on localhost:1080, tun2socks, fixed utun69,
route/ifconfig, and sudoers. The inspected proxy uses `InsecureIgnoreHostKey`.
Do not generalize this snapshot to another SST revision or claim equivalent
scoped DNS/per-VPC readiness without inspecting that code.

SSH-over-SSM corresponds to the transport segment, not routing, DNS, or host
ownership. Root is needed for the chosen transparent macOS routing/resolver
integration, not for every way of reaching a single database. Fixed forwarding,
a proxy-aware client, or a managed VPN can simplify the implementation, with
tradeoffs around unchanged hostnames/ports, TLS, member discovery, and multiple VPCs.

## Validation and safe changes

Follow [testing.md](testing.md) for ordered acceptance and
[spike-coverage.md](spike-coverage.md) for maintained coverage. Record each candidate's
results separately with its source diff, binary digests, OS/architecture/toolchain,
commands, skips, and independent cleanup evidence. Test existence does not imply
candidate acceptance. Native Intel networking requires real Intel testing.

- Run Go lint/vet/unit/race and bounded parser fuzzing directly through Go.
- Python tests cover selection, installation, orchestration, and actual process
  interoperability; pytest never compiles Go. Set `STLV_TEST_TRAFORO_IMAGE` for interop.
- Supply candidate wheel/sdist paths for archive exclusions and isolated-wheel tests.
  Enable actual release downloads explicitly with `STLV_TEST_TRAFORO_RELEASE_DOWNLOADS=1`.
- Native host mutation requires `--integration-macos-privileged -n 0`; AWS adds
  `--integration-tunnel`. Other tiers cannot be combined. `run_all.sh` runs neither.
- Upgrade acceptance compares the verified current release with a distinct local
  candidate. Preserve active traffic on refusal and prove fresh two-unit DNS/TCP
  after replacement. Exercise staging, retirement, and publication interruptions.
- Host acceptance covers normal exit, owner/parent death, helper restart, 100 cycles,
  foreign resolver refusal, cross-venv isolation, and cleanup/uninstall separation.
- AWS acceptance proves Function URL → local handler → verified DocumentDB TLS,
  member discovery and read/write; standalone access, reconnect, partial failures,
  and command shutdown. Normal exit retains bastions. Application teardown removes
  them; audit AWS absence and restore the original helper/host baseline afterward.

Preserve current wire/carrier/control/journal formats and independent vectors.
Never weaken peer identity, descriptor ownership, session capabilities, generation
fencing, or uncertain-state retention to simplify tests. Do not erase journals or
foreign resources to obtain a passing result. Linux/WSL/Windows remain future work;
see [linux-plan.md](linux-plan.md).
