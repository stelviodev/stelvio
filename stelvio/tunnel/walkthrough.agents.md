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
supervisors and discovery. The nonroot Go forwarder owns DNS serving/probes. Local socket → route/utun → root helper →
revocable Unix datagram carrier → nonroot Traforo forwarder (tun2socks/gVisor)
→ OpenSSH SOCKS → private plugin SSM WSS → SSM agent → loopback forwarding-only
sshd → private service. Database TLS remains end-to-end. DNS: system resolver →
scoped /etc/resolver entry → Traforo DNS relay → SOCKS/SSH TCP → VPC resolver (+2).

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
The split repository and release CI are at
`https://github.com/stelviodev/traforo`. `pkg/traforo-client-go` is authoritative for the Go client API and unit tests and
is mirrored by the repository split. It is excluded from wheel/sdist. Release CI
remains external; scripts/build.py is a monorepo developer build/install tool.
`assets.py` pins v0.0.7 URLs, GitHub asset IDs and architecture SHA-256 values.
`ensure_traforo()` locks, validates and atomically caches a download under
`get_bin_path()`. `GITHUB_TOKEN`, when provided by the environment, authenticates
only the GitHub API request; cross-origin redirects drop authorization. Otherwise
use the pinned public download URL. Production never calls `gh`. Both helper and
forwarder still execute the same root-owned installed image; forwarder checks its
selected digest without a cache or network operation. Managed startup additionally
validates the binary version before installation. New images use one semantic
version. Older output does not satisfy the activation gate; trusted older helpers
remain eligible for safe retirement. Normal uninstall also works offline.


## Answering a reviewer

The HTML has nine chapters: Architecture, Trace, Components, Principles,
Comparison, Code, Alternatives, Go DNS, Evidence. Admission selectors demonstrate
readiness versus disabled bypass. The trace stepper distinguishes deploy/dev/start.
Component columns explain necessity, removal loss, and SST equivalent. Requirement
cards defend each adapter; alternatives toggles show when fixed forwarding is
simpler. Source excerpts have SHA-256 hashes of their displayed source lines; refresh
excerpts and hashes after edits.

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
artifacts. Local source builds require the explicit environment override and must
not be presented as the pinned release. Historical native/Go results above predate this distribution change.

Go unit tests also run directly against the authoritative monorepo module.
Python release/download tests cover authenticated and
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


## Local Go developer escape hatch

`uv run python pkg/traforo-client-go/scripts/build.py` builds only this Mac's
architecture, validates the local image, and installs it through the existing
privileged replacement transaction. It prints an exact export command for
`STLV_TRAFORO_DEV_BINARY`. Run that in each shell before `stlv dev` or
`stlv tunnel start`. There is no persistent root marker. The script cannot export
into its parent shell. Unset the variable and run `stlv tunnel install` to restore
the pinned release. Older Stelvio versions do not recognize this override.

`assets.select_traforo` centralizes selection; pinned download verification remains
unchanged. Developer images must be absolute user-owned regular files, with no
symlinks, extra hard links, foreign write modes, special mode bits or extended ACLs.
Validate Mach-O/size/protocol, digest and unchanged inode/permissions/bytes before
installation. Invalid overrides fail closed. `NetworkSession.start` uses the captured
context environment for installation; the isolated runtime inherits that same
snapshot. Forwarding checks the selected digest and launches the root-owned installed
image. Ordinary inspect/uninstall need neither the override nor its file.

Go source/API/unit tests here are authoritative. The split repository owns release
builds and Go implementation CI. Use Go directly for unit/race/fuzz tests; Python
builder tests mock compilation. `STLV_TEST_TRAFORO_IMAGE` may select the same local
image for explicit process interoperability tests. No source, script or executable
is packaged into wheel/sdist, and release checksums/caches are never overwritten.

Local developer acceptance (2026-10-17): Go unit/race/fuzz, 485 affected Python tests
including actual local-image runtime/forwarder interop, native local/pinned switching,
active refusal, interrupted recovery, two-unit DNS/TCP and missing-file offline
uninstall passed. Source ACL and test portability findings were fixed. Original
idle helper and owned host state restored. New Apple identityservicesd utun5/utun6
were independently attributed and preserved; global interface inventory differs for
that reason, not an owned cleanup failure. Evidence /private/tmp/traforo-local-proof.
No AWS run or native Intel acceptance is claimed for this developer change.

## Go DNS migration (2026-10-17)

`internal/dnsrelay` now owns DNS wire handling (miekg/dns v1.1.72), scoped UDP/TCP
listeners, SOCKS upstream exchanges, direct-host payload probes, and observations.
`internal/forwarder/control.go` is a bounded JSON control-v2 interface with request
IDs. DNS configure/reject/retire and probes/observations carry unit, host generation,
attempt and revision. DNS views survive packet removal, retain their port through
reconnect, reject queries during outages, and are capped at eight including retained
views. Hostnames are exact matches; only configured domains authorize descendants.

Python supervisor retains discovery, monotonic helper resolver claims and retries.
It receives the Go listener port before configuring the helper, asks Go to prove
SOCKS and ordinary host paths, and uses an isolated OS getaddrinfo subprocess with
Go arm/check/release observations. Never replace these checks with SOCKS-only
readiness. No production Python DNS parser or dnspython dependency remains.

At migration time, `capabilities` reported forwarder=2/dns=1. The semantic-version
build change below supersedes that handshake. Managed startup validates the
captured selection before installer/host changes and checks the installed
forwarder directly without downloading again. STLV_TRAFORO_DEV_BINARY is optional
for local development. During the historical migration, v0.0.4 lacked them and
required a local build. No release publication or CI changes were made here.
Existing historical acceptance predates Go DNS unless stated.

## Go DNS acceptance and next step

Historical local-build migration: Python 485 passed; Go unit/race + bounded DNS/control fuzz passed;
native two-VPC scoped DNS/carrier/reconnect, observed fail-closed DNS, crash/restart,
active refusal/interrupted replacement and offline uninstall passed. Both architecture
build checks and Rosetta DNS/control tests passed; real Intel networking is pending.
AWS stlv-b37960/test on default/us-east-1 passed in 1389.96s including teardown:
public URL/local TLS/member discovery/read-write, standalone/reconnect and SIGKILL.
49 resources deleted; independent absence audit passed; 59 metadata versions,
one log group and passphrase removed. Original helper, exact interface/resolver
baseline and empty/certain host state restored. See testing.md for exact hashes
and repeatable testing-team steps. Historical tables above remain historical.

That acceptance used v0.0.5. The current pin is v0.0.7; local builds remain optional. Future native/AWS
acceptance must record the selected release digest separately from local-build evidence.
Do not silently restore the Python DNS relay or let v0.0.4 activate networking.


## v0.0.5 release pin acceptance (2026-10-17)

Both actual release assets were downloaded through the production downloader with
a test-process environment token. SHA-256, Mach-O architecture, approved system
libraries, version, capabilities and cache reuse passed. Arm64 is 7,772,690 bytes;
x86_64 is 8,237,176 bytes and ran under Rosetta. Both report forwarder=2/dns=1.
334 affected Python tests passed, including real release DNS/runtime interop.
Startup now validates the selected release or developer image before installation;
the forwarder validates capabilities on the root-owned installed image without a
download. The developer override is optional. No helper install, host networking
acceptance or AWS deployment was performed for this pin update. Prior local-build
AWS evidence stays historical; native Intel networking remains pending.

Wheel and sdist exclusion checks passed: neither contains Go source, developer
build scripts or executable assets; wheel metadata has no dnspython requirement.


## Unified GoReleaser builds and semantic versions (2026-10-18)

The release workflow and developer builder use GoReleaser v2.18.3 and the same
`.goreleaser.yml`. Read that file for flags, cgo settings, macOS 15 minimum,
architectures, artifact names and linker injection. The script captures Go source,
config and workflow, supplies original checkout Git metadata, and runs
`goreleaser release --snapshot --clean --skip=publish,announce` without credentials.
Both binaries and build.json publish together under a unique ignored build bundle.
It installs only the host image, unless `--build-only` (also used by make build).
Rosetta validates amd64 on arm64; Intel statically checks cross-built arm64 and
records native execution pending. A failed build never replaces an earlier bundle.

`traforo version` now prints only vMAJOR.MINOR.PATCH. CI derives it from its tag;
local snapshots use the pinned base plus `-dev+g<12-character-commit>`, independent
of Stelvio's tags. Source fingerprints distinguish uncommitted edits. No new
capabilities command or DNS handshake field exists. Internal helper/carrier,
control framing and journal schema identifiers remain intact.

Stelvio accepts the exact pinned base, plus its snapshots only with the developer
override. Historically, while v0.0.5 was pinned, its published legacy output
was accepted only for its known architecture hashes. The current v0.0.7 pin
rejects that output for activation; the frozen upgrade fixture remains separate. No old arbitrary developer
image receives legacy acceptance. The root-owned installed forwarder is checked
directly using the captured selection, without downloading. Inspect/cleanup/
uninstall remain usable offline. Bootstrap retirement accepts owned semantic-version
images as well as owned legacy installations, preserving receipt and resource checks.

Reviewer checks: GoReleaser shared flags and pin; captured source/config mutation
checks; both artifact paths from artifacts.json; snapshot version/commit metadata;
pre-install version gating; digest-bound legacy acceptance; developer-only snapshots;
installed image digest agreement; build-only never authorizes. Historical capabilities,
local-build AWS and release acceptance above describe the images tested at those dates.
This change introduces no new AWS or privileged host acceptance.

Validation for this change: real GoReleaser arm64/amd64 build passed; native arm64
and Rosetta version/library/architecture checks passed. Go lint/vet/unit/race passed;
350 affected Python/CLI tests passed with the developer image, including actual
DNS/runtime interoperability. Both actual pinned legacy images remain accepted.
Wheel/sdist exclusions and documentation build passed. No privileged installation,
AWS deployment, release publication, commit or branch change was performed by this
implementation. Exact bundle/source/binary hashes are in testing.md's 2026-10-18 record.


## Maintained regression tiers (2026-10-18)

Read `spike-coverage.md` for the mapping from every historical proof family to
maintained tests or obsolete policies. Go owns the large two-VPC stream/half-close
regression and parser algorithms. Pytest owns actual installed-binary/OS/CLI
interoperability. It must not compile Go.

All host mutations require `--integration-macos-privileged -n 0`; AWS scenarios
also require `--integration-tunnel`. `run_all.sh` never enables these tests.
The host fixture locks across UIDs, preserves/restores the installed image and
checks independent routes/interfaces/resolver/service/process inventories.
100-cycle, command/parent death, helper restart, foreign resolver replacement,
interrupted upgrades and migration are maintained tests. Legacy and cross-venv
artifacts are explicit prerequisites. Missing artifacts produce pending acceptance.

Canonical application fixtures live under `tests/integration/fixtures/tunnel_app`;
all generated ownership/evidence uses pytest temporary directories. Historical
spikes remain preserved and are not executable instructions for the current code.
Record current-run results in testing.md/task log separately from historical results.

Current acceptance: macOS15.7.5 arm64 native suite12passed, including100cycles,
real legacy migration and cross-venv use. Both refined safety cases passed again.
All4AWS scenarios passed across two runs using default/us-east-1; the initial run's
authorization timeouts remain recorded as failures. Retry4passed includes2AWS and
2native cases, teardown and original-helper restoration. Fresh audits verified all
proof-owned AWS resources/metadata absent and both host baselines restored.
Evidence roots: `/private/tmp/stelvio-aws-regression-20261018` and
`/private/tmp/stelvio-aws-regression-retry-20261018`. Read testing.md for exact
artifacts, hashes and summaries. Go lint/vet/unit, bounded fuzzing and full
amd64/Rosetta race passed; native ARM64 race is blocked by the pinned gVisor CAS
assembly defect and fails early. Real Intel networking remains pending. Do not
describe the pinned release as carrying the local DNS listener collision fix.


### 2026-10-18 dependency update: unit-only acceptance

Updated miekg/dns to v1.1.73, tun2socks/v2 to v2.7.0, x/sys to v0.49.0,
x/net to v0.61.0, x/time to v0.16.0, x/exp to
v0.0.0-20261014143641-f0251eb4aa3e, and testify to v1.12.1. btree remains
current at v1.1.3. Go tooling regenerated go.sum and removed obsolete entries;
its retained YAML v3.0.5 and x/sync v0.24.0 checksums are current as well.

The latest gVisor main revision (9e9495997e29) failed compilation because its
Bazel source layout mixes Go package names. The corresponding Go-consumable
branch revision, v0.0.0-20261017122056-1b7906fdee8c, compiles without Traforo API
changes and includes the corrected ARM64 CAS new-value register R2. The
platform fail-fast guard was removed without weakening the stream regression.

On macOS 15.7.5 arm64 with Go 1.27.2, both complete suites passed (all six
packages), including the native ARM64 large-stream race regression:

```sh
cd pkg/traforo-client-go
go test -mod=readonly -count=1 -timeout=120s ./...
go test -mod=readonly -race -count=1 -timeout=180s ./...
```

The go.mod and go.sum SHA-256 digests were identical before and after both runs.
This supersedes the historical ARM64 race blocker for local source only. No
release pins, installed helper, host networking or AWS resources were changed;
no release build or native networking acceptance was performed. Real Intel
networking remains pending.


## Handover: acceptance gaps closed in tests (2026-10-18)

The remaining runbook checks now have maintained regression tests. This section
records the new procedures; execution results are appended separately below.

### Artifacts and installed distributions

Build artifacts before invoking pytest. Tests never compile Go or build wheels:

```bash
uv build --out-dir "$TRAFORO_RUN/dist"
export STLV_TEST_STELVIO_WHEEL="$TRAFORO_RUN/dist/stelvio-<version>-py3-none-any.whl"
export STLV_TEST_STELVIO_SDIST="$TRAFORO_RUN/dist/stelvio-<version>.tar.gz"
uv run pytest tests/tunnel/test_distribution_acceptance.py -q
STLV_TEST_TRAFORO_RELEASE_DOWNLOADS=1 \
  uv run pytest tests/tunnel/test_release_acceptance.py -q
```

Use the actual absolute artifact filenames. The distribution tests inspect both
archives for excluded Go source, native build scripts and executable payloads, then
install the wheel into an isolated temporary venv. Imports and CLI checks run
outside the checkout with `python -I`, including unsupported-platform/non-VPC
checks and absence of Stelvio's former `dns` dependency. The database acceptance
venv separately installs PyMongo, which may bring its own dnspython dependency.

Release tests use real production downloads into a test-owned cache, verify both
pinned hashes, Mach-O architectures, size, approved direct libraries and cache
reuse, and execute compatible versions. On Apple Silicon, amd64 runs through
Rosetta. Unsupported execution is a named pending skip after structural inspection.
Live downloads are disabled unless explicitly enabled; mocked tests retain HTTP
failure, authentication and redirect coverage. Only an existing environment
`GITHUB_TOKEN` authenticates production requests; never log it.

### Native and AWS additions

The native cross-venv test now verifies installed-package provenance using
isolated Python outside the checkout. Its wheel prerequisite remains explicit.
The real release-to-candidate test requires `STLV_TRAFORO_DEV_BINARY` selecting a
locally built image with different bytes from the independently frozen v0.0.5 upgrade baseline. It installs
the verified release, proves two-VPC DNS/TCP still work across an active upgrade
refusal, upgrades while idle, then repeats routing/DNS/carrier checks on the
candidate. Missing or identical candidates leave this acceptance pending.

Native replacement interruption tests cover staging before retirement, retirement
before publication, and publication before activation. Only test-owned bootstrap
copies receive injected exits; production fault switches are not introduced.
The host fixture records raw `scutil --dns` output, compares normalized resolver
order, domains, endpoints, options and interface scopes, and allows ten seconds
for dynamic-store convergence. Only resolver numbering and reachability lines
are ignored. Raw mismatch evidence is retained.

The single-VPC AWS tests now separately exercise standalone SIGINT and SIGKILL.
SIGINT must produce exit code zero and `Tunnel stopped.`, empty/certain helper
state, absent owned transports, unchanged deployed state and the original running
bastion. A new standalone session then exercises abrupt command death.

Run the existing exclusive native and AWS commands above, serially. Keep the same
candidate override in every test process, supply the candidate wheel, preserve
the original installation, and require independent host/AWS restoration. Never
claim skipped legacy fixtures, Rosetta execution or historical results as native
Intel acceptance.


### 2026-10-18 acceptance-gap checks: non-AWS results

These results apply to the local pre-v0.0.7-pin candidate, not a newly certified
external release. Go source, release pins and CI/CD were unchanged by the test
additions. The AWS run is recorded separately after teardown.

- Affected Python tunnel/CLI tests, including archive inspection, isolated wheel
  imports, and actual local-image DNS/runtime interop: **376 passed**, with the two
  explicitly disabled live-download cases skipped.
- VPC/DocumentDB/Function-VPC unit regressions: **158 passed**.
- Real v0.0.5 release downloads and cache checks: both architectures passed. The
  final release/DNS-evidence check passed **12 tests** (2 assets, 10 comparisons).
- Go lint (zero issues), vet, fresh readonly unit and native arm64 race suites:
  all passed. Bounded fuzzing passed: request 844,425, DNS 100,310, control 184,186,
  snapshot 387,702 executions. Dependency file hashes remained unchanged.
- Both local GoReleaser artifacts built and validated: arm64 7,789,378 bytes;
  amd64 8,270,120 bytes, executed through Rosetta. Version
  `v0.0.5-dev+g401781924d4f`; source fingerprint
  `9bc4149909df9269de1ab1ba45d4b25c23cf3fe6cb733fed5fe632f1b97beffe`.
- Native macOS 15.7.5 arm64: **14 passed, 1 legacy-artifact skip** in 271.62s.
  Refinement rerun: **4 passed** in 34.31s, covering all three interruption receipts
  and real C-helper migration. The supplied legacy digest matched the independently
  recorded historical task-log digest. All 15 distinct native scenarios passed.
- Both native runs restored the original helper and matching interfaces, routes,
  resolver files, normalized effective DNS and launchd service baseline. Evidence:
  `/private/tmp/traforo-gap-native-20261018` and
  `/private/tmp/traforo-gap-native-refined-20261018`.
- Wheel/sdist and build records: `/private/tmp/traforo-gap-distributions` and
  `pkg/traforo-client-go/build/traforo-ctu1lkl7/build.json`. Test review found and
  corrected graceful-exit false positives, unsupported-ISA structural checks,
  active-upgrade traffic assertions, DNS convergence and fault-receipt checks.

Native Intel networking remains pending. These local-image results do not imply
that the external v0.0.5 binaries contain the newest local dependency fixes.


## 2026-10-19: completed pre-pin AWS acceptance

The already-running default-profile / us-east-1 suite completed **4 passed** in
6146.40s: single-VPC `True`, single-VPC `{}`, multi-VPC partial failure/crash
recovery, and actual non-VPC CLI invocation without an installed helper. These
results use the earlier local `v0.0.5-dev+g401781924d4f` candidate; they do not
certify v0.0.7 host networking or AWS behavior.

Normal standalone SIGINT and abrupt SIGKILL were exercised separately. Normal
session exit retained deployment-owned bastions. Proof application teardown then
removed all four applications: `stlv-59b458`, `stlv-42c7ce`, `stlv-b91958`, and
`stlv-231546`. Independent AWS absence and transport audits passed for each.
The final teardown waited for AWS-managed Lambda ENIs; none were deleted manually.
Independent host inspection confirmed the original helper digest
`fd65dee8e07f72f0303a51140a7b1158dcfa11f5cc6856ebbb675a21c1516625`,
empty/certain helper state, and restored routes, interfaces, resolver files,
normalized effective DNS, launchd service and networking-process baseline.
Evidence: `/private/tmp/traforo-gap-aws.log`,
`/private/tmp/traforo-gap-aws-20261018/independent-audit.json`, and
`/private/tmp/traforo-gap-aws-20261018/macos-host0/independent-final.json`.

## 2026-10-19: v0.0.7 pin validation

Only after the preceding suite and independent teardown audit completed, Stelvio's
release pin, URLs, API asset IDs, SHA-256 digests and derived cache paths changed
to v0.0.7. Networking requires exactly `v0.0.7`; development snapshots
`v0.0.7-dev+g<commit>` require `STLV_TRAFORO_DEV_BINARY`. Old semantic versions and
old legacy output cannot activate networking. Safe retirement still accepts owned
older installations. The genuine upgrade fixture downloads its independently
frozen v0.0.5 baseline from
`tests/integration/fixtures/traforo-upgrade-baseline.json`, regardless of the pin.
Its verified download/version smoke passed; the privileged upgrade acceptance
above predates this fixture extraction.

- Affected tunnel/CLI Python suite: **380 passed, 5 skipped** in 21.94s, including
  actual v0.0.7 process DNS/runtime interoperability. Skips were the separately
  enabled distribution/release tests, all run successfully below.
- Real release downloads: **2 passed** in 4.93s. Both pinned digests, Mach-O
  architectures, size bounds, approved system libraries, semantic versions and
  verified cache reuse passed on macOS 15.7.5 arm64. amd64 executed through Rosetta.
  arm64: 7,822,402 bytes; amd64: 8,311,080 bytes.
- Wheel/sdist and isolated installed-wheel tests: **3 passed** in 10.42s. The wheel
  loaded outside the checkout with the exact current version and architecture
  manifest; neither archive contains Go source, developer build scripts, native
  executables or the Session Manager plugin. No former dnspython requirement.
- Evidence: `/private/tmp/traforo007-final-python.log`,
  `/private/tmp/traforo007-final-release.log`,
  `/private/tmp/traforo007-final-packaging.log`,
  `/private/tmp/traforo007-final-assets`, and `/private/tmp/traforo007-final-dist`.

Go implementation and release pins other than Traforo remain unchanged. No new
AWS deployment, privileged helper installation, Go build, branch change or commit
was performed for the pin update. Earlier native/Go/AWS results remain separate;
real Intel networking acceptance is pending.
