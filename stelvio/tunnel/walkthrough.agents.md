# VPC dev mode: reviewer and implementation handover

Prepared 2026-10-08. This is a companion to [walkthrough.html](walkthrough.html), requested for a fresh agent session. It is a navigation aid and snapshot, not a replacement for the living task record.

## Start here

1. Read [the task record](../../tasks/dev-vpc.md). Read its current header and final acceptance table before historical execution notes.
2. Check `git branch --show-current`, `git rev-parse HEAD`, and `git status --short`. Work stays on `feature/documentdb-vpc-v1`. Preserve unrelated files and user staging; do not commit, push, or switch branches unless requested.
3. Read the walkthrough, then consult the actual sources for the question or change. The HTML is a dated, embedded snapshot; current code wins if they diverge. Requirements/specification define intended behavior; proposals in the alternatives chapter are not implemented features.
4. For implementation work, read [the implementation plan](../../notes/dev-vpc-implementation-plan.md), [requirements](../../notes/dev-vpc-requirements.md), [specification](../../notes/dev-vpc-specification.md), and [declared profile](../../notes/dev-vpc-implementation-profile.md). Older `notes/dev-vpc-plan.md` and branch `feature/documentdb-vpc` are historical reference only. The user explicitly required implementation from scratch, not copying the prototype or treating it as the desired outcome.

Repository: `/Users/sebst/Code/stelviodev/stelvio`. SST reference checkout: `/Users/sebst/Code/anomalyco/sst`.

## What has been done

The feature implements transparent VPC access for locally executed Lambda handlers during `stlv dev`. The target example is [vpc-tunnel-app](../../spikes/vpc-tunnel-app/stlv_app.py). Its public Function URL reaches the local handler through the existing invocation bridge; local PyMongo can use the actual DocumentDB URI, verified TLS, replica-set discovery and read/write.

The task log reports **P0–P6 complete and G6 passed**. P0 selected and proved risky boundaries; P1 added package/policy/contracts; P2 implemented AWS access ownership; P3 shipped the native helper; P4 implemented transport/DNS/multiple VPCs; P5 integrated CLI/admission/shutdown; P6 completed real acceptance. **P7 documentation/changelog, final release reviews and delivery checks remains next.** Creating the walkthrough does not close P7 or constitute a release.

Recorded evidence includes A01–A14, 1033 affected regressions plus five creator-wait tests, 100 actual two-unit native host cycles, cross-venv install/uninstall, installed package checks, reconnection/crash/recovery, private OS DNS, and real CLI → local handler → DocumentDB. The latest multi-VPC/native run reported 2 passed/2 deselected; earlier persistent/omitted-policy runs supply additional evidence. All 14 P6 proof owners were independently audited absent. Helper/service/artifacts were uninstalled and the host network baseline preserved. These are historical results, not assertions about today's machine or credentials.

This reviewer session added only the self-contained HTML and this handover, not runtime changes or AWS resources. The HTML was checked in a browser through a temporary localhost server, which was stopped. Its embedded JSON/JS syntax, 32 verbatim excerpts, source revisions/hashes, work-log hash, and absence of external runtime dependencies were verified. Browser policy prevented direct `file://` verification; the page uses inline content and can be opened locally by the human reviewer.

## Walkthrough identity and navigation

The HTML snapshot records:

- Stelvio branch `feature/documentdb-vpc-v1`, revision `2f3e58906a3ac514db036b7acc4144bdd96c7e2f`.
- SST revision `a0bd20f762883e72a35caccb4896c42ce5b3f707`.
- Full `tasks/dev-vpc.md` SHA256 `cd1780be7317d45b771b5d1edafb6b26def8b9493e53805788a16ed0f1b515e7`.

| HTML chapter/hash | Use it to answer |
| --- | --- |
| `#architecture` | Click Invocation, Database TCP, Private DNS, or Control/ownership, then each component to inspect its role and boundary. |
| `#trace` | Twelve-step replay from installation through response/cleanup; change two-VPC readiness/policy/overlap to explain dispatch decisions. |
| `#comparison` | SST architecture, comparison rows with source links, and ssh-over-ssm differences. |
| `#code` | Search/copy 32 actual contiguous source excerpts, with path, line range, revision and full-file hash. |
| `#alternatives` | Toggle seven requirements; compare current design with five simpler approaches. Ratings are architectural judgments with prerequisites, not live proofs. |
| `#evidence` | Acceptance gates, embedded task-log snapshot, provenance, references and reviewer checklist. |

The HTML needs no CDN, server, telemetry or network requests to render. Optional external source/reference links require connectivity; pinned GitHub links require the revision to have been published. Theme, replay and requirement selections are presentation state only. Print shows all chapters with the currently selected dynamic details. Do not run commands displayed by the page merely because they appear there.

## The architecture, in words

**Invocation plane:** `stlv dev` deploys Python stub Lambdas and starts the local Python bridge. The remote stub and local bridge establish outbound AppSync Events WebSockets. The stub publishes event/context/endpoint/request identifiers, with chunking for large payloads. The bridge reassembles, checks admission/deadlines, runs the real handler in an executor thread, and returns a correlated result. The stub matches/reassembles the response and returns it to the public URL caller. The dev Lambda constructor omits VPC attachment; ordinary deployment retains it. AppSync carries invocation JSON, not database packets.

**Database TCP plane:** The handler opens ordinary sockets to real private DocumentDB addresses. macOS routes enabled VPC CIDRs into helper-owned utun interfaces. A root native C service retains the kernel descriptors and exports a revocable Unix datagram packet carrier to the nonroot Go forwarder. Go uses tun2socks/gVisor to translate IPv4 TCP into SOCKS streams. Ordinary-user OpenSSH `-D` opens corresponding remote connections from the access EC2 instance. SSH bytes travel through AWS Session Manager; the SDK starts `AWS-StartSSHSession`, and a fixed adapter executes `session-manager-plugin`, which implements its own WSS data channel. This WSS connection is distinct from AppSync. Database TLS remains end-to-end through the streams.

**Private DNS plane:** Helper-owned `/etc/resolver` files direct scoped host/domain queries to the local Python DNS listener. It accepts local UDP/TCP but forwards upstream using **TCP through SOCKS/SSH** to the owning VPC resolver (primary CIDR network address + 2). DNS bypasses Go's packet translation. Validated discovery supplies cluster/member names and configured private domains. Unrelated DNS retains the existing system path; private failures must not fall through to unrelated public answers. SystemConfiguration is inspected for conflicts, not modified by this helper.

**Control/ownership plane:** The CLI captures startup provider/home context and launches an isolated `python -I` networking child before running the bridge. The child imports no app code and owns AWS sessions, transport, discovery, DNS and per-VPC supervision. A control socket communicates readiness; a separate lifetime pipe makes parent EOF observable. The native launchd helper is installed globally by fixed OS-authorized bootstrap, independent of the installing venv. It checks kernel peer identity, PID birth, capabilities and generations, and journals owned host effects. AWS intent/claim/effect records and actor fencing support crash recovery. Isolation protects state from handler environment/module mutation; it is not a hostile same-user-code sandbox.

**Access policy:** Omitted/`None` means automatic temporary session-owned access; `True`, explicit configuration, and dictionaries including `{}` mean persistent app-owned access; `False` disables managed access and warns. Temporary access uses a separate Pulumi stack/checkpoint and durable ownership records. Normal dev exit removes temporary access and owned host effects but retains the application and explicit persistent bastion. Disabled access bypasses managed admission; it does not guarantee database reachability.

Each enabled VPC has independent transport/readiness. New dependent invocations are rejected before handler dispatch while that VPC is unavailable; healthy VPCs and plain handlers continue. Reconnection creates fresh transports and permits new requests without replay. Overlapping enabled CIDRs/conflicting host ownership are refused. Host routing also affects other local processes targeting those CIDRs: it is not limited to the handler's sockets.

## Source map for questions and changes

Paths below are relative to repository root unless marked SST. Use `rg` to locate symbols; line numbers in the HTML belong to its snapshot.

| Concern | Start with |
| --- | --- |
| CLI entry, startup and finalization | `stelvio/cli/__init__.py`, `stelvio/cli/commands.py` (`run_dev`) |
| Stub packaging versus ordinary VPC Lambda; local execution | `stelvio/aws/function/function.py` |
| Invocation transport, chunks, dispatch | `stelvio/bridge/remote/stub/function_stub.py`, `stelvio/bridge/local/`, `stelvio/bridge/_chunking.py` |
| VPC public policy / metadata contract | `stelvio/aws/vpc.py`, `stelvio/tunnel/policy.py`, `manifest.py`, `aws_inventory.py`, `stelvio/aws/document_db.py` |
| Child isolation, admission, watchdog | `stelvio/tunnel/runtime.py`, `runtime_child.py`, `session.py`, `engine.py` |
| AWS temporary access and recovery | `stelvio/tunnel/access_*.py`, `actors.py`, `processes.py`, `process_child.py`, `recovery.py`, `storage.py` |
| Bastion security / ephemeral authentication | `stelvio/tunnel/bastion.py`, `transport.py`, `credentials.py`, `ssm_proxy.py` |
| Per-VPC readiness / discovered endpoints | `stelvio/tunnel/supervisor.py`, `discovery.py` |
| DNS and SOCKS | `stelvio/tunnel/dns.py`, `socks.py` |
| Helper install / protocol / host authority | `stelvio/tunnel/installation.py`, `helper_client.py`, `helper_protocol.py`, `wire.py`, `native/*.c` and `*.h` |
| Kernel peer, routes, packet carrier, resolver ownership | `native/ownership.c`, `interface.c`, `route_reply.c`, `packet*.c`, `pump.c`, `resolver_*.c`, `unit.c`, `journal*.c`, `snapshot.c`, `state_store.c` |
| Go translation / coherent packaged assets | `stelvio/tunnel/forwarding.py`, `forwarder/main.go`, `forwarder/build.py`, `native/build.py`, `assets.py`, `_assets/` |
| Real example / strict PyMongo client | `spikes/vpc-tunnel-app/stlv_app.py`, `functions/ping.py` beneath that example |
| SST invocation bridge / AppSync auth | SST `platform/functions/bridge/bridge.go`, `cmd/sst/mosaic/aws/appsync/appsync.go`; follow local worker dispatch from those paths |
| SST tunnel / install / bastion | SST `pkg/tunnel/proxy.go`, `tunnel_darwin.go`, `cmd/sst/tunnel.go`, `platform/src/components/aws/vpc.ts` |

## Answering reviewer questions without overclaiming

- **Why so much code?** Full requirements include transparent member discovery and scoped DNS, multi-VPC isolation, no public SSH, independent temporary ownership, nonroot app/networking, bounded native privilege, crash recovery and cross-venv installation. Separate essential networking from extra ownership/security/product requirements. Do not claim all this code is necessary for a one-host demo.
- **Is it just `aws ssm start-session` in a thread?** Stelvio already delegates the SSM wire protocol to AWS's plugin. A remote-host port-forward session gives one local port → one remote host/port. It does not supply host routes, replica-set members, scoped DNS, admission, ephemeral access ownership or native cleanup. A thread also shares the handler-mutated process environment; the current child captures and preserves startup context.
- **Why not localhost:27017?** PyMongo discovers additional members. A single endpoint forward does not route those addresses. Rewriting a TLS endpoint to localhost also changes hostname verification unless separately engineered. Do not propose disabling certificate verification as the fix.
- **Can PyMongo just use SOCKS?** Do not infer support from the MongoDB driver specification. The inspected PyMongo 4.18.2 rejected `proxyHost` with `connect=False` in a local check. A SOCKS-only design needs a supporting client or an adapter and DNS integration; it changes the transparent-client requirement.
- **What is privileged?** Only the installed C helper owns macOS network authority. CLI, handler, networking Python, OpenSSH, AWS plugin and Go run as the ordinary user. Go receives a carrier, never the raw kernel utun descriptor. General UDP forwarding is rejected; local DNS UDP support is a separate relay.
- **How secure is SSH?** Host identity is obtained through a fixed SSM command and pinned; OpenSSH uses strict host-key checking. Instance Connect provides ephemeral client authorization. Bastion sshd is loopback-only, the forwarding user is nonroot, and interactive sessions/PTY are disabled (`MaxSessions 0`). Current bastion has a public IP for outbound access, with no inbound SSH rule. It is not an entirely private-subnet/no-public-IP design.
- **How does SST compare?** The inspected SST also uses AppSync Events/WSS for live invocations, with a Go Lambda Runtime API proxy/local workers and SigV4 AppSync authentication; Stelvio's bridge is Python/API-key based. SST's tunnel uses direct public TCP SSH, local SOCKS, tun2socks, fixed `utun69`, and installed Go under sudo. Its sudoers grant includes `NOPASSWD:SETENV` for tunnel-start arguments; the inspected SSH callback uses `InsecureIgnoreHostKey`. The standalone bastion defaults to public TCP22 ingress; customization/NAT reuse can differ. The inspected command selects one completed tunnel entry and lacks the equivalent scoped DNS relay in those tunnel files. Do not generalize this to every SST deployment, claim SST cannot resolve any private name, or imply SST was live-tested here.
- **How does ssh-over-ssm compare?** Its project wraps SSH through SSM and temporarily authorizes keys through RunShellScript (15-second window); Stelvio uses Instance Connect and pinned host identity. Its minimum README example disables strict host-key checking. The script supplies transport, not transparent host routing/DNS/ownership. Recheck [the project](https://github.com/elpy1/ssh-over-ssm) before describing changed upstream behavior; this comparison is a snapshot.
- **What was the simpler viable MVP?** One explicit user-managed bastion plus one SSM remote-host forward, with deliberately reduced database behavior and a separately correct TLS endpoint arrangement. For transparent sockets, evaluate sshuttle or an existing VPN rather than assuming a one-port forward is equivalent. sshuttle needs remote Python/command sessions (current `MaxSessions 0` policy would need changing), local routing privilege and carefully scoped DNS. External VPN delegates routing/DNS/authentication but still needs AWS SG/connectivity setup and forfeits automatic temporary ownership unless added. A smaller privileged Go design saves the native split but expands root code/dependencies/authority. None of these proposals was live-deployed in this reviewer session.

## Evidence boundaries and known traps

- Supported/proven profile is macOS 15.7.5 arm64 / Darwin 24.6.0, Python 3.12.11. Do not present Linux/Windows/general VPN support as delivered.
- Read final passing evidence before earlier failures; retain failures as failures. The 70-second plain-handler proof established the same local PID/UID/marker and successful response, not known remote Lambda-container reuse.
- The example's older comment describing `bastion=True` as dev opt-in is stale: omission/None now also enables temporary access. The walkthrough calls this out; do not silently repeat the comment as policy.
- Shared bootstrap/home and unrelated resources were preserved in cleanup. Name/tag matching alone is not sufficient authority for deletion. Failed or uncertain cleanup must retain ownership/recovery evidence.
- DocumentDB AZ capacity failed in earlier fixtures. The successful fixture used supported AZ override `us-east-1a,us-east-1f`; this is a capacity workaround, not a change to production database class/TLS/discovery.
- AWS Lambda ENI deletion can take 45 minutes; the fixture destroy bound was increased to 3600 seconds. Do not manually delete AWS-managed ENIs or call a slow teardown successful before final summary and independent absence audit.
- Work-log evidence lives under `build/p6/`, notably `final-package.json`, `snapshot-native.json`, `native-cycles.json`, `final-host-absence.json`, and per-owner records. Check existence before relying on a local artifact; these may be ignored/local-only. Earlier phase evidence is linked by the task/notes files.

## Continuing implementation safely

1. Establish current state from the task header, plan, source and working tree. A reviewer question is not authorization to implement an architectural replacement. If asked to simplify, first state which requirements are retained or relaxed and update the design rationale accordingly.
2. Map the affected boundary using the source table; trace its callers and invariants before editing. Preserve strict TLS, original hostnames, no replay, captured credential-provider context, per-VPC admission, ownership fences and foreign-state preservation unless the user explicitly changes the contract.
3. Use repository skills applicable to component/source/test work. Run focused tests matching the change; broaden for shared CLI/component/bridge contracts. Typical local entry points are `uv run pytest tests/tunnel/ -q`, relevant `tests/bridge/` and CLI/component suites, and `uv run ruff check` / `uv run ruff format --check` on changed Python files. Inspect native tests before running: some compile fixtures; actual host-authority proofs are a separate privileged lane.
4. Native/Go source changes require coherent assets and manifests. Build as an ordinary user on the declared host with `uv run python -m stelvio.tunnel.native.build` and/or `uv run python -m stelvio.tunnel.forwarder.build`; inspect the builders and installed-artifact checks first. Never put compiler/package-manager/project Python execution in the privileged installer. Validate a built wheel/sdist outside the editable checkout; source-only unit success is insufficient.
5. For a requested real proof, read the manual-testing and running-integration-tests skills and current harness. Historical authorization used the default AWS profile and required cleanup; verify the current request and environment before creating billable infrastructure. The exclusive lane is `STLV_TEST_AWS_PROFILE=default uv run pytest tests/integration/ --integration-tunnel -n 0 -v`, with `STLV_TEST_AWS_REGION`, `STLV_TEST_TUNNEL_PYTHON` and optional `STLV_TEST_TUNNEL_AZS` configured deliberately. It requires the matching approved helper and supported host. Do not combine tier flags or run parallel host-authority sessions.
6. Complete teardown and exact-owner absence audits for app/access resources, SSM sessions, zones, logs, metadata versions/passphrases, and host effects. On failure, follow durable recovery; do not erase records to produce a green result. Keep evidence and update `tasks/dev-vpc.md` plus its existing `todo.md` line when implementation/proof state changes. This companion remains a snapshot; avoid a second competing task ledger.
7. If completing P7, use the docs/changelog/review workflows and plan's G7 criteria. This HTML is a reviewer tool, not a substitute for user documentation or release validation. No commit/push is currently requested.

## Maintaining the HTML

The file is handcrafted inline HTML/CSS/JS plus a `<script id="source-data" type="application/json">` payload. It contains `samples`, `gates`, `workLog`, hashes, branch and revisions. The narrative/model lives in JS structures such as `components`, `paths`, `steps`, `comparisons`, `requirements` and `designs`; updating only an excerpt can leave the explanation or simulator stale.

- Refresh changed samples from **actual contiguous file lines**. Recompute their full-file SHA256, path/line range, repository revision and pinned URL. If sources are uncommitted, explicitly label that state; never attach a commit revision to content it does not contain.
- Refresh gates from the current final acceptance table and work-log hash/snapshot together. The existing snapshot ends before `### Earlier P6 execution notes`; it does not embed the entire historical log. A changed task hash alone does not invalidate unchanged source excerpts.
- Escape JSON `<`, `>` and `&` (for example as `\u003c`, `\u003e`, `\u0026`) so source text cannot terminate the script block. Render source text with escaping/textContent, never execute embedded examples. Keep all assets inline and external links optional.
- `/private/tmp/build-vpc-walkthrough.py` was a one-time generator and may disappear. It expects the original placeholder, which has already been replaced, and cannot simply be rerun on the finished page. Do not depend on it in a new session; update the JSON block deliberately or create a durable reviewed generator if needed.
- Validate JSON and executable JS syntax; compare every embedded sample to its current source line range and full-file hash. Browser-check chapter navigation, component/source links, replay, outage/overlap/policy simulator, search, alternatives presets, checklist and theme. The simulator must distinguish plain handlers (no networking needed) from disabled database handlers (user-provided access needed).
- To preview, serve this directory on loopback with `python -m http.server PORT --bind 127.0.0.1 --directory stelvio/tunnel`. Use browser tooling according to its policy; stop the temporary server and close temporary tabs afterward. Avoid adding runtime fetches or requiring a server for the delivered artifact.

## Suggested next-session behavior

For reviewer Q&A, start in the matching chapter, locate its excerpt, inspect current surrounding code, and answer with the mechanism, tradeoff and evidence limit. For a requested code change, resume the living plan at the relevant boundary and record the new state there. With no new implementation request, do not launch AWS proofs or automatically begin P7 merely because it is listed as next.
