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
| `#principles` | Requirement-by-requirement complexity defence, five scope scenarios, and clickable adapter chain. |
| `#approaches` | Explicit Stelvio/SST simplicity, capability, security and operational tradeoffs; selectable lenses and dimensions. |
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

## First-principles extension — 2026-10-09

Checked Stelvio `6455368bbb78fd12571f65969c9af22f5091ff2d` and SST `a0bd20f762883e72a35caccb4896c42ce5b3f707`. Only the two walkthrough artifacts changed; no runtime/AWS proof changes. All 32 original excerpt hashes still match the source files. Original source/proof snapshot identities above remain historical; the new assessment has its own provenance in `firstPrinciples`.

### How to use the new section with a reviewer

Open `#principles`. Five scenario buttons progress from a fixed endpoint to a normal cluster, multiple clusters in one VPC, multiple VPCs, then the full accepted workflow. They highlight relevant decision rows; they do not compute a minimal architecture. Click a layer in the adapter table to open its reasoning, then source buttons to inspect code. Native accordions let the reviewer expand multiple decisions together.

Use the six-part argument below: requirement/rationale → consequence → implementation → defence → SST → simpler alternative/tradeoff. Distinguish logical necessities from explicit security/product requirements and selected mechanisms. Requirements/specification define authority; initial plan/profile explain chosen mechanisms. R13 explicitly leaves internal split/IPC/forwarding open. Do not defend the current implementation as uniquely minimal.

The key corrections to the example hypotheses are: same hostname does not inherently require privileged DNS; DNS answers and network paths are independent. Ordinary DocumentDB names may already resolve to private IPs outside the VPC, whereas associated private zones need the right DNS view. OS routing/scoped resolver changes require privilege in our host-wide implementation; the DNS listener remains nonroot. Also, one DocumentDB cluster with normal replica discovery is not the same problem as one fixed endpoint. Multiple clusters in one VPC can share one SSH/SSM transport; multi-VPC isolation is the larger scope multiplier.

### 01. Unchanged client code, hostname, port and verified TLS

**Authority:** R01, R06; plan P4.

**Requirement / rationale:** A dev-only URI or disabled certificate check tests a different connection contract. Keep the actual hostname for TLS identity and the actual service port; preserve normal client options too. This provides network parity, not the production Lambda IAM identity or per-function SG isolation (R07).

**Consequence:** The hostname must resolve to an address the local client can reach. Name resolution and reachability are separate obligations. Same hostname does NOT logically imply root DNS access: a public DNS answer may already contain the correct private IP, or a preconfigured VPN/container resolver can supply it. In this host-wide macOS design, changing routes and scoped resolver files requires elevation; the DNS server itself does not.

**Stelvio:** Keep the linked URI unchanged; route real private IPs through utun. Publish scoped /etc/resolver entries when a VPC DNS view is needed. The Python DNS listener binds a nonprivileged ephemeral loopback port.

**Defence:** This preserves the application contract and hostnames used by strict TLS. A hosts-file rewrite is not sufficient to create a network path and is not inherently needed for ordinary DocumentDB names.

**SST:** SST keeps ordinary socket destinations through CIDR routes, utun and tun2socks. The inspected tunnel has no equivalent scoped DNS relay; existing name resolution may suffice for names available outside the VPC, but that does not establish private-zone support.

**Next simpler alternative:** Use an existing correctly configured VPN plus DNS: fewer Stelvio components, external setup/ownership. For a fixed endpoint, map its original name to a loopback address and forward the original port while preserving TLS verification; this adds privileged mapping and does not solve dynamic members automatically.

**Complexity judgment:** Adds host routing and, where necessary, DNS policy. Neither C nor Go nor SOCKS is dictated by hostname parity alone.

**HTML source sample IDs:** Stelvio `uri`, SST `sst-routes`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 02. One DocumentDB: distinguish one endpoint from one cluster

**Authority:** R06; A02; plan P4.

**Requirement / rationale:** One database cluster is not necessarily one socket destination. A replica-set client can discover member hostnames even when the initial URI contains only one cluster endpoint.

**Consequence:** A fixed-endpoint demo can use a single forward. The accepted one-cluster workflow must also reach discovered members with unchanged URI/options and verified TLS.

**Stelvio:** Use a VPC-wide TCP route and validated cluster/member discovery rather than a single -L listener. The same path serves cluster and member connections.

**Defence:** The extra mechanism is justified by normal discovery and failover semantics, not by the mere count of databases. AWS explicitly warns against replica-set mode through its one-endpoint SSH-forward example.

**SST:** SST also forwards a routed address range over SOCKS/SSH, so it is not limited to one predetermined -L destination. This is an architectural capability, not an SST DocumentDB proof from this session.

**Next simpler alternative:** ssh -L or AWS-StartPortForwardingSessionToRemoteHost to one endpoint. Deliberately reduce the contract to a direct connection and engineer hostname verification. Changing URI/options gives up R06; do not disable verification to hide it.

**Complexity judgment:** Port forwarding is the right small solution for a relaxed single-endpoint contract. It is not equivalent to the accepted single-cluster contract.

**HTML source sample IDs:** Stelvio `discovery`, SST `sst-proxy`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 03. Multiple DocumentDB clusters in one VPC

**Authority:** R06, R07; plan P4.

**Requirement / rationale:** An application can link several databases, usually on the same service port. Each can publish its own changing set of members.

**Consequence:** Connections need destination-specific addressing; a single localhost:27017 listener cannot select multiple remote databases. Database count does not require one bastion or SSM session per database.

**Stelvio:** One access path/SOCKS endpoint for the VPC; route its CIDRs and discover the supported resources. Each TCP connection supplies its actual destination IP and port.

**Defence:** Dynamic forwarding makes transport grow with VPC count instead of maintaining a listener, local port and mapping for every cluster/member. SG rules still must authorize each service.

**SST:** SST uses one SOCKS proxy with a Dial callback to the chosen SSH host, forwarding multiple addresses within its routed subnets.

**Next simpler alternative:** Several -L forwards over one SSH connection, with distinct local ports and explicit client config; simpler for a small fixed inventory, but URI changes violate parity. To preserve same ports, distinct loopback addresses plus hostname mappings/listeners can be engineered; membership refresh, aliases and cleanup return as custom code.

**Complexity judgment:** Several databases alone do not force TUN. Several databases plus unchanged clients and changing topology make dynamic routing much less brittle.

**HTML source sample IDs:** Stelvio `forwarder`, SST `sst-proxy`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 04. Multiple databases across distinct VPCs

**Authority:** R11, R09, R02; plan P4/P5.

**Requirement / rationale:** Each VPC has its own routing, DNS view, access owner and health. One failing VPC must not stop unrelated handlers. The agreed scope explicitly requires this; it is a product scope choice, not a necessity for the original one-database demo.

**Consequence:** Select the correct reachable VPC for each destination and DNS name. Refuse overlapping CIDRs or ambiguous domains: an ordinary IP socket contains no VPC ID to disambiguate equal addresses.

**Stelvio:** Per-VPC access unit, OpenSSH/SOCKS/SSM transport, helper unit, discovery/DNS view and supervisor. Non-overlapping CIDRs select the forwarding stack; manifest dependencies select invocation admission.

**Defence:** Independent units preserve mixed temporary/persistent/disabled policy and healthy paths during partial startup or outage. The packet carrier and helper installation can remain shared.

**SST:** The inspected SST command assigns one completed Tunnel entry to tun, then launches one host/proxy with its subnets. These files do not establish independent simultaneous multi-VPC transport/DNS/health; do not infer a broader product impossibility.

**Next simpler alternative:** Provision VPC peering/TGW or a shared VPN gateway so one access point reaches all networks, then configure routes, SGs and DNS views there. This can reduce local transports but shifts complexity/cost into AWS and broadens connectivity. Or explicitly support only one VPC and remove R11.

**Complexity judgment:** Multi-VPC isolation is a substantial scope multiplier. Cross-VPC centralization is possible, but is an infrastructure redesign, not deletion of a few proxy classes.

**HTML source sample IDs:** Stelvio `supervisor`, SST `sst-command`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 05. Correct private DNS and member discovery

**Authority:** R06, R09, R11; plan P4.

**Requirement / rationale:** Private hosted-zone names exist in the associated VPC view; changing member addresses and aliases must stay accurate. Unrelated host DNS must keep working, and an outage must not silently resolve an owned private name through public DNS.

**Consequence:** Query the right VPC resolver and scope the workstation lookup policy. A static hosts file can cover known names but lacks live DNS aliases/TTL/negative answers and private-zone semantics.

**Stelvio:** Resource discovery plus declared dns_domains drive owned resolver entries. Local UDP/TCP DNS relays over SOCKS TCP to that VPC resolver; generation/health fences reject stale or unavailable private answers.

**Defence:** The relay is needed by the agreed private-zone requirement even if DocumentDB public names already resolve to private addresses. TCP upstream fits SSH forwarding; general UDP support is unnecessary.

**SST:** No scoped resolver publication or VPC DNS relay appears in the inspected SST tunnel files. Its SOCKS server is downstream of OS resolution for ordinary numeric packet destinations; it does not itself install the missing DNS policy.

**Next simpler alternative:** Use VPN-managed split DNS, or user-maintained resolver configuration pointing at a reachable VPC resolver. Fewer Stelvio lines, externally owned lifecycle. Static hosts entries are smaller only for fixed names without general private-zone/refresh requirements.

**Complexity judgment:** DNS relay and OS DNS configuration are distinct. Root publishes system policy; nonroot Python performs requests. DNS bypasses the Go packet path.

**HTML source sample IDs:** Stelvio `dns`, SST `sst-routes`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 06. Ordinary sockets need a path to private IPs

**Authority:** R01, R06, R12; profile forwarding selection; plan P0/P4.

**Requirement / rationale:** The driver opens normal sockets and does not know a SOCKS proxy exists. SSH/SSM byte streams are not a kernel IP interface.

**Consequence:** Some mechanism must bridge ordinary private-IP connections to a remote egress point: routes/TUN, a VPN, OS interception, or application/socket adaptation. TUN is one choice, not a mathematical requirement.

**Stelvio:** macOS route → helper-owned utun → revocable packet carrier → Go tun2socks/gVisor → per-VPC SOCKS endpoint. The userspace stack translates packets into reliable TCP streams.

**Defence:** Reuses a TCP stack instead of writing one, supports arbitrary discovered destinations, and keeps packet parsing/TCP code outside root. General UDP/ICMP/IPv6 are intentionally excluded.

**SST:** SST uses utun69, CIDR routes and tun2socks configured with socks5://127.0.0.1:1080. It chooses essentially the same packet-to-stream boundary, inside the installed privileged tunnel process.

**Next simpler alternative:** Existing VPN or sshuttle removes much bespoke translation code from Stelvio. sshuttle still needs local interception privilege and remote execution/Python (current bastion session restrictions must change). SOCKS-aware clients avoid TUN but alter the client contract; the inspected PyMongo did not accept proxyHost.

**Complexity judgment:** Go is an implementation/dependency choice. The functional boundary is packet-to-stream conversion or an alternative OS/VPN path.

**HTML source sample IDs:** Stelvio `forwarder`, SST `sst-routes`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 07. Dynamic destinations over one authenticated transport

**Authority:** R06, R11; profile selection; plan P4.

**Requirement / rationale:** Every discovered member can require a different TCP destination. Enumerating listeners ahead of time couples transport setup to the database topology.

**Consequence:** The egress transport needs a per-connection destination request. SOCKS5 is a standard interface for that request; it is not additional database encryption or a replacement for TLS.

**Stelvio:** OpenSSH -D serves loopback SOCKS, using SSH direct-tcpip forwarding channels to connect from the bastion. Go and the DNS connector reuse that interface.

**Defence:** Avoids a custom destination/multiplexing server on EC2 and reuses OpenSSH authentication and forwarding. The loopback SOCKS boundary separates userspace packet translation from SSH implementation.

**SST:** SST implements a local SOCKS server in Go; its Dial callback calls sshClient.Dial through the authenticated connection. It does not shell out to OpenSSH -D for that boundary.

**Next simpler alternative:** A custom forwarder could call an SSH library directly and remove the local SOCKS listener/OpenSSH process. It still needs multiplexed destination-aware forwarding, authentication, identity checks and reconnection; code may move into Go rather than disappear. A native VPN removes SOCKS and SSH by replacing the transport.

**Complexity judgment:** SOCKS is convenient and reusable, not mandated by R13. Removing a process is not automatically less code or a smaller privileged/security surface.

**HTML source sample IDs:** Stelvio `ssh`, SST `sst-proxy`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 08. No public inbound SSH on the access instance

**Authority:** R12 explicitly; scope section 2; plan P2/P4.

**Requirement / rationale:** Reduce internet-exposed SSH service and avoid developer-IP allowlist churn. AWS IAM can gate session establishment. This is the agreed security posture, not proof that carefully restricted public SSH can never be safe.

**Consequence:** SSH needs a carrier reachable without a public TCP22 listener. No-public-SSH alone could be met with VPN/private access; the plan additionally mandates SSH over Session Manager.

**Stelvio:** SSM agent establishes outbound AWS connectivity; SDK StartSession and session-manager-plugin WSS carry SSH to loopback sshd. No inbound SSH rule; a public IP currently supports outbound access. EC2 Instance Connect authorizes an ephemeral client key, and a fixed SSM command bootstraps the pinned server key.

**Defence:** Reuses AWS session authorization and the plugin wire protocol, keeps dynamic SSH forwarding, and verifies remote identity independently. SSM replaces how SSH is reached, not SSH forwarding itself.

**SST:** Inspected SST ssh.Dial connects directly to public host TCP; standalone bastion defaults to TCP22 from 0.0.0.0/0. Its host-key callback disables verification. This is simpler and has a different exposure/trust posture; customization may differ.

**Next simpler alternative:** Keep no public SSH and use SSM remote-host forwarding: removes SSH/key/host-key/SOCKS layers for one fixed endpoint, but not transparent topology. Or use a VPN/private SSH route. Restricted public SSH removes SSM/plugin/IAM dependencies but relaxes explicit R12 and requires a deliberate security decision.

**Complexity judgment:** SSM adds agent, IAM, API/session readiness and plugin dependencies. WSS is the AWS plugin carrier, not an extra Stelvio-designed protocol. Session establishment can be audited, but AWS says SSH/port-forward payload logging is unavailable; do not claim full database-content audit.

**HTML source sample IDs:** Stelvio `ssm-plugin`, SST `sst-ingress`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 09. Ordinary-user runtime and bounded host privilege

**Authority:** R04, R05, R10, R12; plan P3.

**Requirement / rationale:** Host route/DNS authority should not also execute app code, AWS credentials, SSH or a third-party TCP stack. Installation must survive removal of a project venv and reject arbitrary privileged commands.

**Consequence:** Separate privileged OS mutation from ordinary-user networking; authenticate the local peer and make ownership/revocation explicit. A compiled C broker is not the only possible small trusted implementation.

**Stelvio:** Globally installed root-owned native launchd service; bounded protocol, kernel peer/birth identity, capabilities/generations and journaled effects. Helper retains raw utun FDs; nonroot Go receives a revocable datagram carrier.

**Defence:** This split directly implements the required privilege boundary and prevents a retained client kernel FD from keeping helper-owned interfaces alive after revocation. It costs IPC, protocol validation and asset/install coherence.

**SST:** SST copies its Go executable to /opt/sst/tunnel and grants sudo tunnel start with NOPASSWD:SETENV; route setup and tunnel run under that privileged process. Less separation, more root networking/library code.

**Next simpler alternative:** Run one installed Go tunnel as root: fewer components, but relaxes the nonroot transport requirement. A small Go helper plus nonroot networking could retain the split, trading runtime/dependency surface for C maintenance. Delegate to a preinstalled VPN service to move the privileged boundary outside Stelvio.

**Complexity judgment:** Native language, launchd layout and carrier protocol are choices; bounded privilege and cross-venv independence are requirements. A simpler proposal must identify whose privileged service replaces them.

**HTML source sample IDs:** Stelvio `helper-peer`, SST `sst-install`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 10. Automatic temporary access, cleanup and crash recovery

**Authority:** R01–R05, R10; plan P2/P3/P5.

**Requirement / rationale:** Default dev should not require manually creating an access server, and temporary development access should not keep accruing cost or widen access after exit. Crash/deletion failure must not destroy app resources or lose ownership.

**Consequence:** Provision and dispose access separately from the app; record intent before effects and reconcile uncertain outcomes. Host effects also need ownership so recovery does not delete someone else's routes/DNS.

**Stelvio:** Separate per-session/VPC Pulumi access stacks, durable intent/claim/effect metadata and birth-fenced actor tracking; helper journals and lifetime EOF revocation. Retain records on uncertainty and recover with exact ownership.

**Defence:** Most of this complexity buys safe lifecycle behavior, not packet forwarding. Reusing the app stack for temporary teardown risks changing its mode or destroying unrelated resources.

**SST:** The compared SST bastion is infrastructure-configured/persistent; the inspected tunnel files do not implement this Stelvio temporary AWS ownership contract. Root tunnel start/stop is not equivalent to crash-safe access-stack reconciliation.

**Next simpler alternative:** Require a preexisting persistent managed instance and user-managed VPN/forwards. Removes access creation/state/actor cleanup from Stelvio, but gives up default temporary provisioning and cost cleanup. A managed VPN may shift ownership to an external service rather than eliminate it.

**Complexity judgment:** This is one of the largest removable complexity budgets if the product accepts manual/persistent infrastructure. It is not justified solely by unchanged hostnames.

**HTML source sample IDs:** Stelvio `access`, SST `sst-command`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 11. Reload-safe transport and per-VPC admission without replay

**Authority:** R08, R09, R11; plan P4/P5.

**Requirement / rationale:** Handlers mutate environment/modules and can be idle. A bound port is not proof of authenticated end-to-end access. Replaying a write after interruption can duplicate effects.

**Consequence:** Keep transport/provider context stable, verify real readiness, block only affected new dispatches, retry connections with backoff, and never hide outages by replaying requests.

**Stelvio:** Isolated python -I child with captured startup context, per-VPC workers/probes and admission state; separate control socket/lifetime pipe; fresh authenticated transports on reconnect. Existing sockets may fail.

**Defence:** A supervised subprocess can suffice for a small design, but a thread sharing handler-mutated environment does not provide this isolation. Readiness/admission and no-replay semantics must survive any simplification.

**SST:** SST uses Go live workers and a tunnel process; the inspected tunnel functions establish SSH/start routes. They do not provide evidence of Stelvio's dependency-specific multi-VPC admission and temporary-owner recovery contract.

**Next simpler alternative:** One supervised forward subprocess with captured immutable configuration, health probe and global startup gate: smaller for one fixed VPC. For multiple VPCs it either blocks healthy handlers unnecessarily or must reintroduce dependency-specific state.

**Complexity judgment:** Process isolation and per-VPC state are separate costs. A thread changes scheduling, not ownership, DNS, route setup, credentials or failure semantics.

**HTML source sample IDs:** Stelvio `runtime`, SST `sst-command`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### 12. Remote invocation must reach local code independently

**Authority:** R07; plan P5; existing bridge.

**Requirement / rationale:** The public URL still invokes a Lambda, while the real handler runs on the developer machine. Local VPC access alone does not send events or responses between those runtimes.

**Consequence:** Keep an invocation/results transport separate from the database network path, and avoid making the dev stub depend on application VPC egress.

**Stelvio:** Existing Python stub/local bridge over AppSync Events WSS with IDs/chunking. Dev stub has no VPC attachment. Local handler sockets use the separate VPC tunnel.

**Defence:** Reuses existing dev messaging rather than tunneling database packets through Lambda/AppSync. This plane existed before the VPC feature; do not charge all its complexity to VPC networking.

**SST:** SST also uses AppSync Events WSS for live execution, with a Go Runtime API bridge/local worker transport and SigV4 authentication. Its SSH tunnel is separate.

**Next simpler alternative:** For manually invoked local code, omit the remote invocation bridge, but give up the actual AWS URL/event-source workflow. A direct localhost dev endpoint is a different product contract. Changing SSM cannot remove AppSync while keeping the existing workflow.

**Complexity judgment:** There are two unrelated WSS roles: AppSync invocation messages and SSM carrying SSH bytes. Sharing the acronym does not make one redundant.

**HTML source sample IDs:** Stelvio `stub-wss`, SST `sst-live`. Follow the embedded path, inspect current surrounding code, and retain source-scoped caveats.

### Adapter audit checklist

| Layer | Functional obligation | Can be removed/replaced when |
| --- | --- | --- |
| Local socket | Application-facing contract; not a tunnel component. | Only remove by changing execution/client contract. |
| Scoped DNS | Select correct name → address view; separate side path. | Omit if all names resolve correctly already; private-zone R06 then needs another provider. |
| Routes / utun | Get unchanged private-IP sockets out of the kernel. | Replace with VPN/interception or client adaptation. |
| Go TCP translation | Convert IP packets to connection-oriented streams. | Replace with existing VPN/sshuttle or another translator. |
| SOCKS5 | Express destination IP/port for each stream. | Call an SSH library directly; preserve dynamic channel behavior. |
| SSH | Authenticate and multiplex remote TCP forwarding. | One fixed SSM endpoint-forward can remove SSH; VPN replaces the transport. |
| SSM plugin / WSS | Reach loopback SSH without public inbound SSH. | Private VPN/direct SSH or reduced fixed-endpoint SSM forwarding; public SSH relaxes R12. |
| Bastion → database | Remote egress point plus resource access rule. | Managed network gateway or existing instance replaces it; private connectivity is still needed. |
| Native helper + ownership | Side plane: privileged OS effects and revocation. | External privileged VPN service or larger privileged tunnel; not a TCP hop. |
| Supervisor + access records | Side plane: health, temporary AWS lifecycle and recovery. | Manual persistent setup/reduced failure contract removes much of this cost. |

### Continuing this analysis or implementing a simplification

1. Ask which behavioral requirements can actually change; a request for explanation is not authorization to relax R06/R11/R12. Keep same hostname/port, discovery, TLS verification and private DNS as separate constraints.
2. Compare total lifecycle states and authority, not only process/module count. Fewer Stelvio lines can mean complexity delegated to VPN infrastructure, SSH libraries or privileged dependencies. No measured LOC/performance/cost reduction was established here.
3. If full scope stays, investigate adapter consolidation (direct SSH-library dial instead of SOCKS/OpenSSH) or helper maintenance alternatives as hypotheses, with explicit authentication, revocation and recovery proof. If scope shrinks, remove corresponding acceptance requirements deliberately before adopting a one-port forward/persistent instance.
4. Maintain the `firstPrinciples` JSON items/scenarios/layers and their rendered explanations together; retain original `source-data` proof/excerpt identity unless regenerating it accurately. Update both human and agent explanations when a requirement or conclusion changes. Browser-check scenario highlight, accordion expansion, layer-to-decision navigation and source buttons, plus existing chapters.
5. Recheck primary sources before making new upstream claims. AWS DocumentDB guidance confirms the one-forward replica-set limitation; its disabled-hostname-verification example is not an acceptable implementation for R06. AWS Session Manager documentation distinguishes SSH, fixed remote-host forwarding and shell sessions, and states SSH/forward payload logging is unavailable. Neither SSM WSS nor AppSync WSS is redundant: they carry different data planes.

Primary sources checked for this extension: [DocumentDB outside-VPC connectivity](https://docs.aws.amazon.com/documentdb/latest/devguide/connect-from-outside-a-vpc.html) and [Session Manager session types and limits](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html). Comparisons with SST remain based on the local checkout and are not claims of a fresh SST deployment.

## Explicit Stelvio vs SST assessment — 2026-10-09

Companion HTML chapter `#approaches`, payload `approachAssessment`. Checked Stelvio `e2514d0bbd0079d54a21ec2dc87d86c16f07ea09` and SST `a0bd20f762883e72a35caccb4896c42ce5b3f707`; all original source-excerpt hashes still match. No runtime changes or new deployments.

The central conclusion is scoped: SST’s inspected direct-SSH tunnel core has fewer adapters and persistent-access lifecycle states. Stelvio implements a broader managed contract in DNS, simultaneous VPCs, partial failure, temporary ownership and bounded privilege. SST also has strengths: SigV4 at its invocation bridge and broader platform implementation source. Do not turn this into a claim that either product is universally simpler, more capable or more secure.

Use the Whole system / Simplicity / Capabilities / Security boundaries / Operations lenses to highlight rows. Click or keyboard-activate a dimension to inspect the detailed judgment and source buttons. Highlighting is not a quantitative score. Original `#comparison` remains a mechanism map; this chapter explicitly evaluates the tradeoffs.

### Transparent TCP: broadly the same core

**Stelvio:** Native-owned utun → nonroot Go/tun2socks → OpenSSH SOCKS → SSH-over-SSM.

**SST:** utun69 → tun2socks → Go SOCKS server → direct SSH library dial.

**Where it is simpler:** SST has fewer process/IPC boundaries and no SSM carrier in this path. Both still need packet-to-stream translation and destination-aware forwarding.

**Where it offers more:** Both architectures can address multiple private TCP destinations without per-database -L listeners. No measured throughput or latency advantage was established.

**Tradeoff:** Stelvio buys separation and no public SSH with more adapters. SST is a better reference for a compact single-access-path core, not evidence that a one-port forward meets replica discovery.

**Source sample IDs:** `forwarder`, `sst-proxy`.

### Transport exposure and identity

**Stelvio:** No inbound SSH rule, loopback sshd, SSM plugin/WSS, ephemeral Instance Connect key, pinned host identity.

**SST:** Direct public-host TCP SSH; inspected standalone SG allows 0.0.0.0/0 TCP22; inspected host-key callback is InsecureIgnoreHostKey.

**Where it is simpler:** SST avoids plugin/SSM API/IAM/session readiness and ephemeral-key bootstrap.

**Where it offers more:** Stelvio implements the specified no-public-SSH and verified-server-identity posture. This is a security capability, not more database features.

**Tradeoff:** Stelvio depends on AWS SSM availability, permissions and agent connectivity; SST requires reachable SSH and accepts the inspected identity-verification tradeoff. A restricted public SSH design is possible but differs from both defaults and relaxes Stelvio R12.

**Source sample IDs:** `ssh`, `sst-ingress`.

### Privilege boundary and installation

**Stelvio:** Small C launchd helper owns OS effects and kernel FDs; networking, AWS credentials, Python, SSH and Go remain nonroot; revocable carrier protocol.

**SST:** Installed Go executable /opt/sst/tunnel; sudoers NOPASSWD:SETENV tunnel-start grant; route setup and tunnel run under sudo.

**Where it is simpler:** SST consolidates executable/install/forwarding and avoids Stelvio’s carrier/lease IPC split.

**Where it offers more:** Stelvio has explicit kernel-peer/ownership/revocation boundaries and cross-venv proofs. SST’s installed executable also avoids requiring a project Python venv for its tunnel.

**Tradeoff:** Stelvio maintains C, Go, protocol and coherent assets; SST puts more network/library code and tunnel key context in the privileged process. Smaller authored code is not the same as smaller root authority.

**Source sample IDs:** `helper-peer`, `sst-install`.

### Private DNS and resource discovery

**Stelvio:** Validates DocumentDB cluster/member inventory and configured private domains; per-VPC scoped OS resolver policy; live DNS over SOCKS TCP; outage rejection.

**SST:** No equivalent scoped DNS relay/publication in inspected tunnel files. Existing OS resolution can suffice for externally resolvable resource names.

**Where it is simpler:** SST has fewer DNS/discovery/ownership mechanisms where existing DNS already supplies correct answers.

**Where it offers more:** Stelvio explicitly covers associated private-zone names, member refresh and private-outage behavior in its accepted scope.

**Tradeoff:** Stelvio must track ownership, ambiguity, refresh and cleanup. This extra code is unnecessary if the product explicitly excludes private DNS; it is justified by R06 here. Do not say SST can never resolve private names or that all DocumentDB names require a private relay.

**Source sample IDs:** `dns`, `sst-routes`.

### Multiple VPCs and partial failure

**Stelvio:** Per-VPC access/SSH/SSM/DNS/forwarding/readiness units; distinct CIDRs and domain owners; independent outages and mixed policies.

**SST:** Inspected command selects one completed Tunnel entry and launches its one host/proxy and subnet list.

**Where it is simpler:** SST’s selected single-path model has fewer selection, supervision and recovery states.

**Where it offers more:** Stelvio explicitly supports simultaneous distinct-CIDR VPCs and isolates dependency-specific failures; demonstrated by its acceptance evidence.

**Tradeoff:** Stelvio’s larger state machine is paid for by R11/R09. A shared VPN or peered access network can centralize paths but moves complexity into AWS. Inspected source is not proof of every SST topology limitation.

**Source sample IDs:** `supervisor`, `sst-command`.

### Access provisioning and teardown

**Stelvio:** Omitted policy creates independently session-owned access; explicit True/config persists; durable intents/effects and creator fencing recover uncertain mutations.

**SST:** Compared VPC bastion is infrastructure-configured/persistent; inspected tunnel start/stop is not Stelvio’s temporary AWS-owner reconciliation contract.

**Where it is simpler:** SST’s persistent access model is simpler to operate when the user accepts standing infrastructure.

**Where it offers more:** Stelvio provides automatic temporary creation/removal without destroying the app, including exact-owner recovery and mixed policy.

**Tradeoff:** Stelvio pays substantial lifecycle/Pulumi/provider/metadata complexity and startup/teardown work. Persistent access removes that lifecycle code but continues infrastructure cost/access exposure until independently removed. No cost comparison was measured.

**Source sample IDs:** `access`, `sst-command`.

### Availability, reload and dispatch

**Stelvio:** Captured context in isolated Python child; verified per-VPC readiness; guarded dispatch; fresh transports/backoff; no invocation replay.

**SST:** Go live workers and separate tunnel process; inspected SSH/start functions establish their path, but do not establish Stelvio’s multi-VPC dependency admission contract.

**Where it is simpler:** One independent tunnel process is a simpler isolation arrangement than a multi-VPC protected coordinator with control/lifetime protocols.

**Where it offers more:** Stelvio’s documented/proved behavior includes partial startup, healthy-handler continuity, recovery and no replay. This comparison does not claim SST lacks every retry facility elsewhere.

**Tradeoff:** Stelvio’s probes/admission improve actionable failures while adding state and rejection paths. A simpler design can let socket failures surface to the app, changing readiness guarantees rather than making outages disappear.

**Source sample IDs:** `runtime`, `sst-command`.

### Invocation bridge and authentication

**Stelvio:** Python Lambda stub/local executor over AppSync Events WSS; API-key bridge authentication; existing ID/chunk/deadline handling.

**SST:** Go Lambda Runtime API proxy/local workers over AppSync Events WSS; inspected auth uses AWS SigV4 credential provider.

**Where it is simpler:** No universal simplicity winner: each reuses its ecosystem and existing live infrastructure. The bridge existed before Stelvio VPC access.

**Where it offers more:** SST uses IAM-backed signing for this AppSync boundary; Stelvio’s stronger SSH/helper posture does not imply stronger authentication at every boundary.

**Tradeoff:** Bridge authentication, local language execution and VPC transport are separate design decisions. No benchmark or whole-product security ranking was performed; do not count AppSync as a redundant SSM layer.

**Source sample IDs:** `stub-wss`, `sst-auth`.

### Platform breadth versus declared proof

**Stelvio:** Release assets and acceptance scoped to macOS15.7.5 arm64 / Darwin24.6.0.

**SST:** Local pkg/tunnel implements Darwin and Linux paths. The Windows file is a no-op placeholder (start/install return nil); it does not establish functional Windows tunneling.

**Where it is simpler:** Stelvio’s narrow profile reduces supported-platform work today; SST absorbs platform-specific implementation complexity.

**Where it offers more:** SST has implemented Linux coverage beyond Stelvio’s declared macOS profile. Windows is a placeholder here, and no fresh cross-platform acceptance was run.

**Tradeoff:** Stelvio can make a precise narrow support claim, but more users/platforms remain unsupported. Its evidence depth on this host cannot substitute for SST-style platform breadth.

**Source sample IDs:** `install`, `sst-routes`.

### Evidence depth versus implementation size

**Stelvio:** A01–A14 in the task log, real CLI/TLS/discovery/read-write, two-VPC outage/recovery, native ownership cycles, installed-package and cleanup audits.

**SST:** Source checkout inspected, not deployed or benchmarked during this review.

**Where it is simpler:** SST’s visible core is simpler; we did not measure total repo LOC, maintenance cost or correctness probability.

**Where it offers more:** Stelvio has concrete evidence for its chosen contract; this is confidence in a bounded implementation, not evidence that it is the best design for every user.

**Tradeoff:** Many tests/evidence records reflect deliberate failure/lifecycle scope. Simplification should remove requirements intentionally or rerun their proofs; fewer modules alone does not demonstrate preserved behavior.

**Source sample IDs:** `watchdog`, `sst-command`.

### Reviewer and change guidance

- Separate transport-core simplicity from total product behavior. Both retain TUN/TCP/SOCKS/SSH; SSM/plugin, scoped DNS and ownership are additional obligations, not accidental duplicates.
- Compare security at each boundary: Stelvio pins SSH identity and narrows root authority; SST’s AppSync uses IAM/SigV4. Never infer a whole-product winner from one boundary.
- Describe SST private DNS/multi-VPC/lifecycle differences as absence of equivalent machinery in inspected files, not proof of impossibility throughout SST. Darwin/Linux have actual implementation paths; Windows methods are no-op placeholders. File presence alone is not functional support or a tested-platform claim.
- Label dynamic-IP/strict-TLS/multiple-destination support as architecture where no SST live proof was run. Keep Stelvio historical acceptance results distinct from this current source-only assessment.
- Do not invent numeric LOC, latency, throughput, reliability or cost rankings. Consolidating processes can move code or enlarge root authority rather than remove obligations.
- When changing a comparison, update `approachAssessment.rows`, the rendered chapter and this agent explanation together; validate source references and keyboard/mouse selection. Changing a lens must not hide contrary evidence or imply a score.
- A reduced scope should explicitly relax requirements before removing their mechanisms. For full scope, assess direct SSH-library adapter consolidation against existing authentication, privilege, revocation and lifecycle acceptance gates.
