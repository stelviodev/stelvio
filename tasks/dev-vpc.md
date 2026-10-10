# Basic VPC dev implementation

Status: Traforo consolidation complete on feature/documentdb-vpc-v1-traforo. Native acceptance passed on macOS15.7.5 arm64; x86_64 Go tests/commands passed under Rosetta, real Intel networking pending. Default-profile actual example/TLS/member discovery/read-write/transport recovery passed; both proof owners independently audited absent. Helper uninstalled, exact host baseline restored.
Next step: release certification of the exact Go1.27.2 artifacts; P7 user docs/reviews/package checks complete. Intel native acceptance and other OS backends remain deferred. No commit/push/branch change. Preserve user staging.
Last touched: 2026-10-10.

## P7 documentation and delivery checks — 2026-10-10

- Completed the public guide in `docs/docs/concepts/dev-mode.md`: Traforo wheel
  assets and macOS prerequisites, ordinary-user installation, local dependencies,
  automatic session startup, per-VPC admission, routing/DNS boundaries, reconnect,
  shutdown, exact printed AWS recovery commands, and matching-package upgrades.
- VPC guide now documents temporary/persistent/disabled policies, custom private
  DNS and access costs/lifetimes. DocumentDB guide describes local credentials,
  unchanged endpoint/TLS/replica settings and CA paths. CLI reference and
  troubleshooting cover current commands rather than prototype flags.
- Removed stale "coming soon" and prototype failure claims; updated actual example
  README and comments while preserving fixture replacement strings and both
  persistent/omitted acceptance cases. Contributor guide and runner comments
  describe the four exclusive serial tunnel cases, macOS15.7.5 fixture, clean-wheel
  PyMongo dependency, recovery ownership, teardown and independent audits.
- Added concise unreleased changelog entry; no new pages/nav changes, concrete AWS
  price figures, runtime changes, automated builds, commits, branch changes, or
  staging modifications. User staged documentation during this turn; preserved it.
- Sequential source then test review: no remaining blockers. Acted on source
  finding that unavailable VPC invocations return immediately rather than queue.
  Clarified unsupported custom Pulumi-provider credentials/endpoints/proxies.
- Verification PASS: Zensical site build; 889 affected tunnel/bridge/CLI/VPC/DocDB
  tests; one disposable mocked execution check covering four new infrastructure
  examples (harness removed). Ruff on example passes with its existing INP001 and
  ARG001 exclusions; runner shell syntax and staged/unstaged whitespace checks pass.
- Wheel/sdist built in `/private/tmp/stelvio-p7-dist`; installed into a clean venv
  outside the project. Both architecture assets and source/digest coherence PASS;
  arm64 and Rosetta x86_64 `version` PASS; tunnel/recover help PASS. Simulated Linux
  and Windows imports work and reject managed assets; non-VPC/bypass behavior is
  covered by the affected tests. Both inventories contain Traforo assets/manifest.
- Both walkthroughs now point to the completed user documentation and retain dated
  historical acceptance. P7 documentation/review/package deliverables are complete;
  G7 release certification must not imply native/AWS acceptance of the exact
  Go1.27.2 binary: that rerun remains unverified, as does real Intel networking.
  No native installation, privileged host mutation, or AWS resources created here.

## Go module/toolchain update — 2026-10-10

- User requested go.mod Go1.27.2 and module `github.com/stelviodev/traforo`.
  Updated every internal import, gofmt and release compiler pin; dependency
  versions unchanged. Stayed on existing `feature/documentdb-vpc-v1-traforo-p7`.
- Downloaded Go1.27.2; all Go race packages PASS. Both pinned release assets rebuilt
  and manifest refreshed.25 asset/install Python tests PASS; x86_64 version through
  Rosetta PASS. Current sizes: arm64 6,333,282 bytes, x86_64 6,669,472.
- Walkthrough current snippets/build/size information updated. Earlier native,
  fuzz and live AWS acceptance below is historical Go1.25.3 evidence, not a rerun
  for this compiler. No native installation/AWS deployment or resources created.
  No commits, branch changes or staging modifications.

## Traforo consolidation — 2026-10-10

- In progress on existing `feature/documentdb-vpc-v1-traforo`; no branch switch,
  commit, or push. Replace production C helper with Go policy/state/service and
  narrow Darwin cgo API adaptation, combined with existing Go forwarder.
- One precompiled executable per architecture inside the wheel: macOS arm64 and
  x86_64, macOS15+. Preserve two processes/root boundary and ABI/carrier/journal
  formats. Linux/Windows/WSL backends and CI/CD rebuild automation deferred.
- Intel native networking proof explicitly deferred by user; architecture/build
  checks required now. Additional macOS versions are targets, not historical
  acceptance evidence. Stripped builds,16MiB asset bound, report actual sizes.
- Legacy helper must be cleaned with its matching package; no automatic upgrade.
  Keep old C implementation until Go parity passes. Default AWS proof authorized,
  all proof-owned resources must be removed and independently audited.
- Go helper/service/admin/protocol/journal/resolver/unit code implemented, narrow
  Darwin cgo bindings; forwarder migrated. Initial race tests passed;216 independent
  historical golden vectors now run Go and pass. Two installer recovery defects
  found by code reviewer and corrected. Release builder pins Go1.25.3 and clears
  overrides. Two stripped assets ~5.7MiB/5.9MiB; Intel version runs via Rosetta.
- Python loader/installer/forwarder wiring migrated. `uv sync --frozen` restored
  dnspython. Old C/assets retained until parity. User staged some working files
  during the turn; preserve index, do not commit or overwrite staging.
- First two native install attempts failed (no AWS resources created): proc_pidpath
  buffer needed first-NUL termination, and O_EVTONLY cannot open Unix sockets for
  ACL inspection. Corrected bindings and added tests. Failed proof image/service
  cleanup is exact digest/inode constrained; second cleanup47985 succeeded.
- Earlier next-step checklist (now superseded by evidence below): rebuild assets,
  native proofs/tests, installed wheel, live AWS proof, cleanup and walkthroughs.

- Final source parity: migrated independent request/peer/IO fixtures and native
  route/sysctl harnesses. No runtime/test dependency on the superseded C tree;
  removed production `native/`, old standalone `forwarder/` and old assets.
  Snapshot v3 codec/transitions and authorization/generation policy are portable
  Go; live peer, Unix carrier, host filesystem/admin/event loop stay Darwin-only.
  Future Windows owner encoding requires a new journal version, not Unix handles
  embedded in shared policy. No future backend stubs added.
- Review fixes: shared production journal-prefix classifier; independent path and
  FD ACL assertions; invalid snapshot returns zero-state assertion; native FD
  inventory; direct generation/authorization/lease/shutdown tests. 750 affected
  tunnel/bridge/CLI tests PASS; Go race all packages PASS; bounded fuzzing10s each
  PASS (~1.51million request and1.08million snapshot cases). Ruff PASS.
- Both assets rebuilt with Go1.25.3, SDK15.5, Apple clang17.0.0
  (clang-1700.0.13.5), readonly/trimpath/noVCS/stripped flags. Mach-O and approved
  direct libraries checked; x86_64 all Go tests and version PASS through Rosetta.
  arm64:5,936,050bytes, wheel contribution2,259,226bytes; x86_64:6,164,424bytes,
  wheel contribution2,389,010bytes. Legacy arm64 total8,356,418bytes,
  compressed4,237,793bytes; new dual-arch compressed4,648,236bytes (~9.7% more).
  Both assets under16MiB. Wheel and sdist inventories PASS, no old executables.
- Initial Go native routing/DNS/active-uninstall/peer/ancillary/release proof and
  SIGKILL/partial-journal/recovery/capability-rotation proof PASS on macOS15.7.5
  arm64. Helper removed through matching-artifact cleanup before final rebuild.
  Final wheel installed from venv A; A moved away, B installed separately;
  repeating acceptance with B. Native Intel networking and other macOS versions
  remain untested (15+ target is not acceptance on every release).

- Final wheel cross-venv native proof:80dcdf28-df9a-460f-9025-60e5e58d2edf PASS;
  restart proof:c6a000c7-e7a8-493e-be85-c73c4ae15e86 PASS. Installed forwarder
  invoked as root refuses execution before descriptor/network work PASS.
- Live owner cc22e8 reached Network READY using final installed-wheel code, but
  first invocation failed because the isolated proof venv lacked the example's
  PyMongo dependency. This is a failed run, not acceptance. Installed PyMongo4.18.3
  in venv B; fixture cleanup is running before repeat. No implementation change.

- cc22e8 failed only for missing application dependency; fixture teardown completed
  (24m42s total), AWS VPC/cluster/member absence verified, host helper certain/empty.
  The corrected actual-example persistent-policy proof starts only after cleanup;
  no concurrent owner interferes with exclusive native acceptance. Shared helper
  policy/protocol packages cross-build Linuxamd64/Windowsarm64 without cgo PASS;
  this is a boundary compilation check, not an implemented networking backend.
  Cross-venv same-artifact install idempotence PASS.

- Corrected installed-wheel live owner83ad3e: actual public URL → nonroot local
  handler marker/PID → DocumentDB verified TLS/member discovery/read-write PASS.
  Reload/30s idle retained the SSM session; forced interruption caused admission
  failure then attempt2/fresh transport/read-write recovery PASS. Test body passed;
  mandatory teardown/independent audit is still running, so not final acceptance.

- FINAL: corrected live owner83ad3e finished1 passed/2 deselected in17m43s,
  including fixture teardown. Independent `p6_finalize.py cc22e8 83ad3e` audit
  confirms both owners absent; active AWS resources, SSM access/transports,
  state versions/log groups/passphrases removed. Failed cc22e8 remains failed.
- Final matching wheel B cleanup and repeated cleanup PASS. Installed image,
  launchd plist, state directory and retirement directory absent; launchctl reports
  service absent. Shared preexisting parent directories preserved. Baseline24
  interface names, resolver-file inventory and SystemConfiguration DNS snapshot
  exactly match before/after session AND after uninstall. No proof-owned host/AWS
  resources remain. Evidence under `build/traforo/`: final native/restart JSON,
  AWS absence JSON, host restoration/absence JSON and live pytest logs; detailed
  live ownership/results at `spikes/dev-vpc-v1/build/p6/{cc22e8,83ad3e}`.
- Both walkthroughs updated: current interactive Traforo chapter,41 source
  excerpts (historical C/old forwarder explicitly labelled), current process,
  build/size/platform matrix and agent modification guide. Current snippets match
  full-file hashes/ranges; JS syntax and browser role/source interactions PASS,
  no browser console errors. Temporary browser/server closed.

### Traforo validation matrix

| Platform | Result | Remaining boundary |
| --- | --- | --- |
| macOS15.7.5 arm64 / Darwin24.6.0 | Go race, parser fuzz,750 Python regressions, wheel/sdist, native two-unit TCP/DNS, cross-venv, restart/journal/privilege/uninstall, live default AWS example/recovery/cleanup PASS | No claim of acceptance on other releases |
| x86_64 through Rosetta on that Mac | Both-architecture pinned builds, Mach-O/minOS/library inspection, version and all Go tests PASS | Real Intel networking pending |
| Other macOS15+ | Minimum deployment target15.0 and architecture-aware fail-closed backend | Native acceptance pending |
| Linux/WSL2/Windows | Shared protocol/policy cross-build without cgo | No networking backend or native asset implemented |
| CI/CD | Manual pinned release builder plus stale-artifact validation | Automated recompilation/release deliberately out of scope |


## Sources

- Requirements: [dev-vpc-requirements.md](../notes/dev-vpc-requirements.md).
- Specification: [dev-vpc-specification.md](../notes/dev-vpc-specification.md).
- Implementation plan: [dev-vpc-implementation-plan.md](../notes/dev-vpc-implementation-plan.md).
- Candidate profile: [dev-vpc-implementation-profile.md](../notes/dev-vpc-implementation-profile.md).
- First manual proof: [README.md](../spikes/dev-vpc-v1/README.md).
- AWS ownership proof: [AWS-OWNERSHIP.md](../spikes/dev-vpc-v1/AWS-OWNERSHIP.md).
- Prototype reference: `feature/documentdb-vpc`; inspected at `8045dfc3`.
- Older `notes/dev-vpc-plan.md` is historical design input, not the active plan.

## Decisions and findings

### P6 complete — 2026-10-08

- G6 closed: all A01–A14 have passing evidence below on the declared macOS
  profile. Latest e11c17 complete multi-VPC scenario/native DNS suite:2PASS,
  2deselected,74warnings,3739.43s; both earlier True/omitted cases also passed.
  Final no-helper plain owner a0e11e: actual default CLI, exact local
  PID59025/UID502/marker200 before and after70s idle, no access intents or
  helper installation; proof44452 exit0. Cleanup23 metadata versions and1 log
  group, passphrase absent. Independent audit36024 PASS with0 residual
  versions/logs. This proves post-idle API behavior, not a known reused remote
  Lambda container.
- All14 P6 ownership records have successful independent cleanup-audit.json:
  14933e,2b7b8a,565077,61a84a,6789c2,6a8133,6d8668,7bdc9d,9808d7,d63b54,
  d75096,db32d4,e11c17,a0e11e. AWS app/access resources, active owned sessions,
  private zones, log groups, metadata versions and passphrases are absent.
  Existing shared home/bootstrap and unrelated resources preserved. Final
  host absence check repeated after plain proof PASS: no helper/service/state/
  bootstrap artifacts, resolver baseline/parent inodes and normalen0 routes
  preserved, public OS DNS works. No live proof remains.
- Final validation includes1033 affected regressions plus5 new creator-wait
  tests, native compiled route/snapshot fixtures, 100 actual two-unit host
  cycles, source/significant-test reviews, Ruff and whitespace checks. Final
  package F has207 installed source/assets equal checkout. Last change only
  extends fixture destroy bound to3600; reviewed/formatchecked. No additional
  native rebuild is required; production sources/assets unchanged after final
  package verification. No prototype code copied this implementation session.

- Independent e11c17 audit79035 PASS: app/access effects absent,0 versions/
  0 logs remaining, passphrase absent. Matching packaged F cleanup30325 PASS,
  repeatcleanup exit0/idempotent. Final host check PASS ordinaryUID502:
  helper/service/plist/state/socket/bootstrap/retired artifacts absent;
  resolver entriesempty, shared parent inodes/owners/modes preserved; default
  and10.253/10.254/10.255 routes all normalen0/gateway192.168.178.1; public
  OS DNS works. Evidence build/p6/final-host-absence.json. Actual no-helper
  plainCLI70s idleproof44452 and its exact-owner audit passed afterward.

- Final pytest75058:2 passed,2 deselected,74 warnings in3739.43s (1:02:19).
  e11c17 complete multi runtime/stop/ordinarydeploy AND full fixtureteardown
  PASS. Cleanup104 exact metadata versions,4 Lambda log groups, passphrase
  absent; no errors. Native OS DNS case PASS afterward, session
  1315ee27-0596-4dbf-a31c-e0fceacf034f, private CNAME/outage-public-fallback
  refusal/baseline restoration and certainempty helper. e11c17 teardown
  completed within original40-minute bound; new60-minute bound remains correct
  for documented45-minute subnet waits plus prior deletion phases. Test review
  clear; Ruff checks/format PASS. Independent finalizer79035 PASS.

- Teardown19:53 UTC: both DB instances/clusters gone;28 checkpoint resources,
  three pending deletes (two subnets/one SG), four AWS-managed Lambda ENIs
  remain in opted-out VPC vpc-0041e219ca51470ea. No manual ENI changes.
  Installed pulumi_aws7.47.0/ec2/subnet.py documents provider waits at least
  45 minutes for Lambda ENIs, exceeding harness DESTROY_SECONDS2400. Raised
  future bound to3600 for45-minute subnet wait plus DB/network deletion.
  Running pytest75058 retained its original2400; ultimately completed without
  interruption at19:55 UTC. No manual ENI deletion/recovery was needed.
  Final host baseline captured in build/p6/final-host-baseline.json: resolver
  filesempty, gateway192.168.178.1/en0 (host network changed from earlier68.1),
  shared parent inode/owner/mode. After matching F uninstall, ordinary
  /private/tmp/stlv-p6-host-absence.py passed service/files/markers absence,
  preserved parent/resolver baseline, all three VPC CIDRs using normal routes
  and public OS DNS, saves final-host-absence.json.

Final acceptance evidence — all gates PASS:

| Gate | Passing evidence |
| --- | --- |
| A01 | P3 cross-venv proof; latest E installer moved before B's actual e11c17 dev, same helper image used without reinstall. |
| A02 | True9808d7 and omitted14933e actual no-argument CLI proofs; e11c17 both distinct databases: local UID502/PID/marker, verified TLS/hostname, rs0/member discovery, ping and write/read-back. |
| A03 | Omitted14933e exit/Ctrl+C and ordinary deploy; e11c17 temporary access removed on stop, application retained until deliberate fixture destroy. |
| A04 | P1/P2 policy table and P5 regressions; e11c17 explicit config retained, omission temporary, False no access instance, local opted-out handler200 and dev/ordinary warnings. |
| A05 | P5 default/explicit-auto and unsupported-mode checks; a0e11e actual default non-VPC dev with helper absent, local API200 and no access intents. |
| A06 | True/omitted SSM interruption and e11c17 independent bastion stop/start: only dependent admission closes, automatic fresh transports, new DB invocations, unique invocation IDs/no replay. |
| A07 | True/omitted actual handler reload, idle30s and activity preserve SSM identity/configuration; e11c17 activity throughout both outages. a0e11e no-helper idle70s/API200 with same local PID/UID/marker. |
| A08 | P2/P3 failure/ownership proofs plus e11c17 actual CLI SIGKILL, SDK EOF release, birth-fenced creator disposal, production recover and restart; all14 owned AWS absence audits PASS. |
| A09 | P3 cross-venv uninstall/idempotence/reinstall/fresh session; latest native proof and e11c17 active uninstall refused without disruption. Final matching F uninstall and repeated cleanup PASS; service/artifacts/baseline checks PASS. |
| A10 | P3 actual kernel peer/second process refusal, conflict/stale-generation/capability boundaries; latest snapshot-native proof repeats second-peer refusal. P1/P5 incompatible artifact and prerequisite regressions. |
| A11 | P2/P4 real SSH-over-SSM/host identity/no public inbound SSH and negative security proofs; e11c17 real transport and local UID502; latest native incoming-FD abuse refused. |
| A12 | e11c17 real private PHZ CNAME via OS resolver, discovered database member names, outage rejection and unrelated DNS; latest native 100 two-unit cycles all certain/empty. Post-AWS native private CNAME/public-fallback rejection/baseline restoration PASS, final uninstall leaves no resolver state. |
| A13 | Final F wheel/sdist; 207 installed source/assets equal checkout, isolated package imports/entry points and ordinary UID502 forwarder/bypass verified; uses stelvio.tunnel. |
| A14 | e11c17 multi-VPC test PASSED: both DBs, each outage/recovery, partial startup isolation, mixed-policy stop/ordinary deploy; P1/P3 overlap rejection and latest 100 native cycles. Full fixture teardown and independent physical absence audit PASS. |

Declared profile: macOS15.7.5 arm64/Darwin24.6.0, Python3.12.11,
default AWS account535368238919/us-east-1, ordinary UID502. Latest multi fixture
uses supported Vpc az override us-east-1a/us-east-1f after AWS rejected default
a/b DocumentDB capacity; DB class, TLS, discovery and original example unchanged.
Versions and installed package origin: build/p6/final-package.json. Helper
SHA256 5e3e9539b2bccdd34960d6c57a6f8d39f67f7e885b3a929aaf54e8de5a31f217,
source fingerprint a6e81a3ac1858484637825205dabca4362692b9b04123266ae1e7af6a596bfbb.
Actual native evidence: build/p6/snapshot-native.json and native-cycles.json;
per-owner CLI, invocation, ownership and cleanup evidence: build/p6/<run>/.
Historical failures remain failures; passing acceptance is assembled from the
identified successful cases and subsequent full reruns, not a green relabeling
of session11909.

### Earlier P6 execution notes — historical snapshots

- e11c17 multi-VPC test PASSED at 19:19 UTC, including mixed-policy stop
  (persistent i-0c0cf986eb8008e79 retained, temporary removed) and ordinary
  redeploy with explicit False warning and no opted-out access instance.
  Fixture teardown is running; final pytest summary/native DNS and independent
  physical absence audit remain required before G6 can close.

- e11c17 partialstartup PASS: actual newCLI PID48836 while persistentbastion
  stopped, net1READY/net2retrying; rejectedapi2 beforehandler, healthyDB1 and
  plain/opted exact200/localPIDmarker. EC2start ->automaticnet2READYattempt9,
  fresh localTLS/rs0/member/readwrite DB2 PASS. Phasepartial-startup +
  startup-recovered durableJSON. All required multi runtime behavior now observed;
  stop/mixedpolicy/ordinarydeploy/final fixtureteardown stillpending, noG6yet.


- e11c17 bothoutages PASS: closed admission, healthyotherDB/plain, privateDNS
  outage rejection; fresh automatic transport + TLS/member/readwrite reconnected1
  and2. Second-outage plainHTTP200 with ping fix, no replay IDs. ActualCLI
  SIGKILL + EOFhostrelease + productiontunnelrecover PASS, durable phase
  crash-recovered completeTrue. Commandlog Recovered dd528f71 VPC
  vpc-0cf1fc8c591904fb9. Now devrestart with persistentbastion stopped for partial
  startup; no claim for that remaining phase or finalteardown yet.


- e11c17 actualdev PID46606/session2766525d-334e-41e6-b53c-26606bc2ffdf:
  both DBs TLS/rs0/member/readwrite/localUID502 distinctmarkers PASS; actual
  customprivateOSDNS db.p6-e11c17.internal ->10.254.61.77 PASS; activeuninstall
  refusal preservesbothDBs, plain/opted-out paths and warnings PASS. Runtime
  uses final stub ping_intervalNone source, installed helper5e unchanged.
  Temporary outage currentlyinprogress; no crash/partialstartup PASSclaimyet.


- 2b7b8a pytest18566 final1failed/1passed/2deselected in308.10s. Both
  db.t4g.medium CreateDBInstance rejected InvalidVPCNetworkStateFault: capacity
  onlyus-east-1f, defaulta,b absentcapacity. App failed beforedev; no stubfix or
  crash outcome. Full fixtureteardown PASS; native actualOSDNS afterteardown
  PASS again on5e image. Independent finalaudit60951 pending.
- Added optional fixture-only STLV_TEST_TUNNEL_AZS supported Vpc az list to
  copiedsingle/generatedmulti source. Unset preservesdefault; region/unique
  syntaxvalidated, reprsafeinjection. No instanceclass/DBendpoint/TLS/discovery
  change. Source review clear; generatedAST checks prove az=['us-east-1a',
  'us-east-1f'] in Vpc calls and unchanged DocumentDb constructor; foreignregion,
  empty andduplicate inputs refused. Existing12cleanup/creator tests stillPASS.
  Controlled next live env includesavailablef, remainsdefault/us-east-1.


- Resumed6789 exactcleanup56744 PASS/exit0 after AWS Lambda ENI release waits.
  BothDBinstances/clusters/VPCs removed; actual CliSession.destroy included
  latest log/metadata/passphrase gates. Fresh monitor NoSuchKey mainstate,
  clusters[]/vpcs[], hostownedFalse/uncertainFalse/units0. Independent final
  audit now pending. Retain failed-run evidence; no dev-mode/fix PASSclaim.


- Prepared final no-helper plain CLI proof with strict localUID/PID/marker200
  responses before and after70s idle (two35s waits/progress). This is an actual
  idle check for the stub heartbeat fix, not a claim of a known reused Lambda
  container. No AWS execution yet; current6789 exact cleanup56744 remains live.


- 6789c2 pytest28165 final2failed/2deselected/1teardownerror in510.66s.
  Normalapp deployed, privatezoneZ06905903ENKMYPXSRWB1 created; then public OS
  DNS failed Route53 GetChange, S3/Route53 cleanup and SSM bootstrap. No dev
  started/no localhelper authority. Native case failed its initial publicnip
  baseline beforeacquire, so no native-stage fault or freeze-fix outcome.
  Resources retained134mainstate plus zone, ownerrecordpidNone. API access now
  works in fresh monitor; explicit exact CliSession.destroy resumed from durable
  ownership using /private/tmp/stlv-p6-cleanup-6789c2.py (ordinaryUID), no new
  resources. Do not startnewfixture until complete cleanup/audit. NoPASSclaim.


- d75096 pytest75041 final1failed/1passed/2deselected in1897.95s.
  Failure is exact retainedplain keepalive timeout already addressed in source,
  not run with fix. Teardown completed app destroy/noerrors. Rebuilt native DNS
  counterexample after realAWS teardown PASS, with no repeatstage5. Independent
  latest owneraudit/logs cleanup pending before next fixture. G6 still open.


- Crash recovery harness now waits same SHUTDOWN_SECONDS700 for exact
  birth-fenced SDK creator disposal (production600+30); previous60 could reject
  valid EOF cleanup. Initial claim list/GetObject NoSuchKey race means no
  creator to await, never cleanup success: productionrecover and exact physical
  absence still required. Other errors fail closed. Source/test reviews clear,
  five discriminatory fixture tests PASS; native host unchanged. d75096 app
  teardown underway, native local authority already certain/empty.


- d75096 live multi proof FAIL at second-outage plain FunctionURL assertion:
  retained response500 explicitly says Error waiting for response: sent1011
  keepalive ping timeout; no close frame received. Local handler logged200.
  Temp stop/start + freshTLS/readwrite reconnect PASS; persistent closed
  admission, healthyDB and privateDNSoutage refusal PASS. No crash/startup PASS.
  Fixture teardown currently running, pytest75041; do not replace helper or
  start another AWS fixture until full teardown/nativecase and own audit finish.
- Targeted remote-stub connection fix disables autonomous websocket ping timer
  (ping_interval=None) because Lambda freezes/reuses its event loop between
  invocations. Official Lambda lifecycle and websockets keepalive docs checked.
  Existing auth/staleconnection/response-bound/requestID logic unchanged; no
  retry/replay. Source andtest reviews clear;31stub tests PASS incl exactoption.
  New freeze-dist wheel/sdist built; F verified207sources/assets/UID502 and all
  1033 affected regressions PASS. Native asset/hash
  unchanged5e3e9539...31f217, matching moved installer E retained for cleanup.


- Final cleanup audit exposed AWS-retained Lambda log groups missing from
  earlier absence checks. Removed11 exact Lambda groups belonging to recorded
  completed owners565077/db32d4/9808d7/14933e/d63b54/61a84a; other owners had0.
  All nine completed P6 owners now independently audited/purged including logs,
  metadata/passphrase and all prior app/access effects. Prior audit statements
  were incomplete for logs; latest cleanup-audit.json is corrected evidence.
  Cleanup checks same SDK/account/region, empty checkpoint + physical infra
  absence + no access namespace before any log delete, validates entire exact
  inventory before effects, verifies absence before metadata purge. Source and
  test reviews clear;7 deletion-fence regressions PASS, Ruff checks/format PASS.
  Current75041 loaded old fixture cleanup before this change; run independent
  latest finalizer after its teardown to include currentLambda logs too.
- Current d75096 PID14808/sessione0242481-1815-40c7-8ee6-e333cfb7812c
  both actuallocalUID502 DB TLS/rs0/member/readwrite PASS; customPHZ OS CNAME,
  active-dev uninstall refusal, opted-out/plain PASS. Temporary outage currently
  in progress, dependent admission rejected. Remaining crash/startup cases not
  yet claimed. Old61 plainLambda report shows10014ms on failed last request;
  response body unavailable for that run, no assumption about bridge cause.


- New ordinary-UID502 nativeP3 proof204da392-784a-49d6-969c-975186eba759
  all seven checks PASS on rebuilt5e3e9539...31f217. Clean wheel verified207
  installed source files/assets, UID502 carrier and no-VPC bypass; sdist built.
  Exact10.254/10.253 two-unit stress100 cycles PASS: real OS private CNAME,
  ACTIVE ownership, detach first leaves INACTIVE tombstone and second OS DNS,
  full revocation certainempty each. Test review clear after unconditional
  error/cleanup evidence and mkdir fixes. Initial stress fixture wrongly
  expected removed tombstone absent and failed cycle0, certified cleanup; fixed
  to exact advertised INACTIVE contract. New native OS fallback test PASS0.67s.
  Single-copy snapshot mutation rejects growth case instead of certifying
  expectedowned; bounded retry regression detects it. Stage5 prior cause remains
  unproven; no false root-cause claim. New live d75096/default/us-east-1 running.


- Proof66420/owner61a84a final: 2 failed, 2 deselected in1874.83s. Both
  actual DBs TLS/member/read/write/localUID502 and customPHZ OS CNAME PASS;
  active uninstall refusal, opted-out/plain local paths, ordinaryFalse warning,
  temporary bastion stop/start + fresh DB reconnection PASS. Persistent outage
  closed admission and preserved healthy DB, but plain Function URL returned an
  HTTP error although local log showed200. Old assertion indexed string body,
  producing TypeError. Full status/body now durably recorded and exact local
  response asserted. Persistent reboot recovery/crash/partial startup NOT RUN.
  This was not evidence of an SSM reconnection timeout. Native DNS subsequent
  case failed CONFIGURE stage5. ACK fix alone does not close this failure.
- Application destroy and independent all-owner audit61a84a PASS: no AWS
  resources, access metadata, app history/passphrase remain. Native helper was
  certain/empty and then uninstalled with matching moved final-wheelD.
- Native host snapshot now bounds four fresh size/copy attempts, discards failed
  buffers, retries only copy ENOMEM, and retains strict complete-snapshot
  ownership checks. Source review clear; seven compiled syscall fixtures PASS,
  test review clear; full affected1026 tests PASS. This addresses a concrete
  table-growth race but latest stage5 precise cause is not yet established.
  New native asset built; fresh wheel/sdist snapshot-dist built; clean isolated
  E install pending. No new AWS resource until real native stress is accepted.


- Latest controlled proof66420 running owner61a84a/default/us-east-1, expanded
  3VPC app (2managed + opted-out). Establish ordinary deployment and synchronized
  private-zone/CNAME records BEFORE dev starts, then real2DB/customOSDNS,
  active-dev uninstall refusal, independentstop/start outages, crash/recover,
  held-bastion partial startup/recovery, mixed-policy stop and ordinaryFalse
  warning proof. Two selected cases include native DNS counterexample afterward.
- Added missing ordinarydeploy explicitFalse Pulumi warning in Vpc._create_resources;
  omission/None/config policies remain quiet. Existing publiccomponent policy
  table now asserts exactwarningonlyFalse. Source/test reviews clear;74targeted
  plus final1019 affected tests PASS. Finalwheel rebuilt and206installedsources/
  assets/imports/UID502forwarding PASS again. No unrequested branch/commit/push.
- Fresh normal final-wheelD helperinstall PASS, native proof session
  9366150b-22e6-4710-b93e-1450e5540bd4 PASS: secondkernelpeer refused, active
  uninstall refused, incomingFD abuse rejected, actual2privateOSDNS, actual
  hostTCPcarrier, per-unitdetach preservesotherTCP, fullhostrelease. Evidence
  final-native.json. Dmoved to /private/tmp/stlv-p6-final-wheel-retired-installer
  before B's currentCLI proof; source/helperimagehash remains047ea712...fa2501.
- d63b54 finalFAIL was OSgaierror atcustomPHZlookup; finalpytest1failed/2deselected
  in1674.51s. Fixtureteardown+independentexpandedAWSaudit PASS; noapp/session
  resources, history/passphrase. Native .internal CNAME + publicfallback
  counterexample actualOSPASS0.76s, noAWS. This narrows investigation; it is
  not a substitute for real PHZ. Newfixture waitszone/recordINSYNC beforedev.
- First expanded attempt6d8668 failed beforeAWSdeploy due duplicatedcomponent
  name for optedVpc/Function. CorrectedVpcname toopted-net. Finalfixturecleanup
  andindependentauditPASS,14exacthistoryversions/passphrasepurged. NoPASSclaim
  forwholeattempt. Current61a84a loads correctedfixture.

- Full exclusive run11909 finished: 3 PASS (True9808d7, omitted14933e,
  actual native OS DNS), 1 FAIL (multi7bdc9d route CONFIGURE stage5).
  All three application fixtures completed teardown. Expanded independent
  audits for14933e/7bdc9d PASS; exact metadata/passphrases already purged.
  True and omitted cases include actual reload,30s idle, SSM interruption,
  fresh local TLS/read/write/member/OS-DNS assertions. Omitted ordinary deploy
  creates no temporary access. Never relabel the whole run green.
- Implemented native PF_ROUTE receiver fix: validate common length/version/type
  envelope before route header; skip valid shorter interface notifications;
  accept only exact PID/sequence zero-error ADD within total3s. Final live
  interface/OWNED route reread unchanged. Source review clear. Compiled native
  socket fixtures cover notifications, malformed/truncated/missing/error ACKs,
  wrong PID and sequence. Full affected regression1019 PASS; golden bundle
  digest updated for fixed release source inventory. New helper installed only
  after11909 final summary via matching old-wheel cleanup then normal B install.
- Bastion ready marker now fixed systemd oneshot recreates /run marker each boot;
  cloud-init formerly published it only on initial launch. Source review clear;
  live stop/start recovery remains to prove. No application API/policy changes.
- Current multi rerun d63b54/default/us-east-1, appstlv-d63b54/test,
  sessioncea4fe54-19b7-4fa9-9204-5c7bf2d33bd5. Both paths READY and actual
  Function URLs returned distinct DocumentDB markers with verified TLS,rs0,
  discovered members and UID502. Whole test FAIL at private-PHZ assertion;
  exact traceback pending fixture teardown (session7119 still live). Host is
  certified certain/empty, own application destroy started15:48 local. No
  outage/crash PASS claim. Durable ownership/logs under build/p6/d63b54.
- Final wheel/sdist rebuilt /private/tmp/stlv-p6-final-dist; isolated installed
  package at /private/tmp/stlv-p6-final-wheel-runtime verified206 Python/native
  sources and assets equal checkout; ordinaryUID502 carrier start/stop, imports,
  CLI auto/deferred rejection/recovery entrypoint PASS. Evidence final-package.json.
- Expanded future multi fixture adds opted-out10.255/16 VPC and actual local
  bypass/warnings/no-access checks; after crash, starts dev with persistent
  bastion stopped, proves healthy/opted/plain admission and affected500 before
  handler execution, then recovers. Active-dev uninstall refusal also included.
  These additions are NOT loaded by current7119. Run them subsequently.
  Teardown archives every restart/failed-start access receipt, checks exact
  temporary AWS owner effects/SSM absence before metadata purge. Significant
  test review ongoing; no G6 completion until new assertions run.
- Latest test review clear after exact archive disposal assertions. Route
  regressions mutation-probed outside checkout: restoring old short-notification
  failure and removing sequence fence each violate acceptance assertions.
  Source/installed helper hash047ea7121439fa89d41eaa8a9cdf021130b3def37c2ff4387e136ff114fa2501.
- Added Route53 INSYNC wait before custom OS query plus per-attempt error-type
  evidence, to prevent racing record propagation. New native OS diagnostic
  also exercises .internal CNAME+memberA alongside public-fallback counterexample.
  Corrected its fixture to absolute CNAME target; serialization verified. Real
  native execution waits until current exclusive AWS fixture finalizes.

Remaining G6 execution order: await66420 finalsummary/teardown (61a84a),
diagnose only evidenced failures ifany. Audit all new owners,
normal matching helper uninstall/idempotence, then actual no-helper plain dev
proof and exact app cleanup. Preserve every failed-run record. No root Python,
AWS SDK or transport, no helper replacement while live fixture remains active.

- Latest omitted-policy actual CLI proof ownerdb32d4 PASS: default/us-east-1,
  full pytest summary 1 passed in1503.31s including fixture teardown. Actual
  Function URL reached local ordinary-UID handler, verified DocumentDB TLS,
  member discovery/read/write, OS resolution, automatic SSM reconnection.
  Real logs show affected invocations rejected in retrying before resumed200.
  Final independent AWS audit and50 exact app-history versions/passphrase
  cleanup PASS. Helper certain/empty, application/network resources gone.
- Native diagnostic helper20 local configure/remove/revoke cycles PASS, all
  host disposals certain/empty; no AWS effects. Full tunnel unit suite569 PASS.
  No recurring refusal in latest omitted-policy live activation/reconnection.
  Persistent-only rerun with current helper remains necessary; old refusal
  was not silently recast as PASS or diagnosed from these positive checks.
- Failed owners565077 and6a8133 audited: no app or session-tagged billable
  infrastructure, IAM roles/profiles or active owned SSM sessions;121 exact
  app-history versions and both proof passphrases removed. Bootstrap/home
  preserved. 6a8133 previous suite finished FAIL plus teardown/setup errors
  because helper replacement overlapped its final inspection; no repeat of
  helper changes while live fixtures remain active.
- Significant-test review found and resolved destructive SSM selection,
  crash/failed-start recovery and persistent-only receipt capture. Exact fixture
  target/app/env/principal/start-time/unique AWS receipt is recorded before
  interruption; temporary/mixed owners additionally match intent hash. Teardown
  waits host EOF cleanup and birth-fenced creator death, invokes production
  recover for exact owned namespaces, aggregates cleanup failures and keeps
  unresolved ownership. Purges only certified-empty exact metadata/passphrase.
- New acceptance assertions: handler reload plus30s idle retain SSM identity;
  multi-path outage holds only its own bastion stopped, tests bounded500 with
  unchanged local invocation log plus healthy/plain/unrelated DNS, restores
  bastion in finally and verifies distinct database after recovery. Separate
  real-native public-resolution DNS counterexample now running (no AWS),
  supplementing real custom-PHZ OS lookup. Full latest harness NOT RUN yet.
- Wheel/sdist built successfully with current diagnostic asset into
  /private/tmp/stlv-p6-dist. Clean installed-package verification still pending.

- Preflight default/us-east-1 account535368238919, macOS15.7.5/arm64, one existing
  VPC. Original checkout remains feature/documentdb-vpc-v1, tracked tree clean
  before P6. User authorizes real proofs and cleanup; no stage/commit/push.
- Added exclusive --integration-tunnel lane (-n0/no other tier flags), runner
  opt-in after existing lanes, sibling CLI ownership fixture with durable logs
  under spikes/dev-vpc-v1/build/p6/<runid>/ownership.json. Example source copied
  with unique app identity and local-only marker; original app remains untouched.
- Native helper installed using normal production command/admin bootstrap.
  Installer venv moved; example venv is distinct. Helper cleanup must use B.
- First owner565077/test real CLI READY, actual URL local handler HTTP200, TLS
  read/write/member discovery/local UID/PID/OS DNS assertions passed. Whole test
  FAILED because test SSM reason selector expected UUID36 instead of hex32.
  Terminated history confirms production nonce32. Fixture destroyed49 resources
  exit0; host certain/empty. Not recast as whole-run PASS.
- Selector fixed. Live suite session54597 now runs True owner6a8133 then omitted
  variant; ownership/logs per-owner under build/p6. Source runtime now starts
  protected child in its own process group, so terminal-group Ctrl+C cannot
  interrupt SDK cleanup; harness stop now uses killpg(SIGINT). 13 affected unit
  tests PASS. Pending: live matrix, strict all-owner audit, sequential reviews.
  G6 remains open; helper remains installed until final cleanup from venv B.
- True rerun6a8133 hit terminal host-uncertain at configure, safely removed its
  route and retained DNS rejection. Default gateway unchanged192.168.68.1; no
  VPC/reserved192.0.2.1 route remains. Birth-fenced own CLI group stopped; pytest
  fixture destroying its49-resource app before starting omitted case. Do not
  claim this run PASS. Native foreign-active monitor BrokenPipe is BUSY send/close
  race, separately explained by read-only source review.
- Added strictly bounded authenticated CONFIGURE diagnostic CF/version1/stage
  1..23, keeping all original guards/-1 failure/reload/FD checks. New helper blob
  built nonroot; reviewed no blockers. 39 helper-client tests PASS (incl valid/
  malformed diagnostics); 42 earlier client/supervisor tests PASS. Normal B
  helper upgrade in progress. Diagnostic must identify repeated native refusal
  before G6; no speculative ownership relaxation.
- Safe installer refused replacing a different installed helper; matching old
  package in retired installer venv uninstalled it, then example venv B installed
  reviewed diagnostic blob successfully (session54796 exit0). Shared root parents
  preserved. Updated helper is currently installed; final teardown remains required.
- Harness now also has multi-VPC scenario (temporary + persistent config),
  distinct database markers, non-VPC endpoint, custom private zone/OS DNS, and
  crash/recover command. It is NOT RUN yet. CLI evidence and invocation IDs are
  retained in future owners; original running2-case pytest loaded prior harness
  snapshot, so final complete scenario run is still required.

### Controlled manual P6 release execution

This is the macOS acceptance execution path; the existing Ubuntu job does not
claim this lane. Operator uses macOS15.7.5 arm64/Darwin24.6.0 with lid open,
ordinary UID, AWS CLI/session-manager-plugin/SSH and the agreed AWS profile.
Confirm no other host tunnel and enough VPC quota for three simultaneous fixture
VPCs (two managed paths and one explicit opt-out). Existing installation must be the approved artifact and have no owned or
uncertain state. Never overwrite an unrelated helper to prepare the run.

1. Build `uv build --out-dir /private/tmp/stlv-p6-dist`, install wheel into a
   clean venv, verify both packaged assets/source, then use ordinary
   `stlv tunnel install` from that venv and the fixed admin bootstrap. Move the
   installer venv and use a separate runtime venv without reinstalling.
2. Run exclusive lane with no other tier flags:
   `STLV_TEST_AWS_PROFILE=default STLV_TEST_AWS_REGION=us-east-1 caffeinate -i
   uv run pytest tests/integration/ --integration-tunnel -n 0 -v`.
   Four cases: persistent example, omitted example, multi-VPC mixed-policy
   outage/crash/PHZ, and native OS DNS public-fallback counterexample.
   `STLV_TEST_TUNNEL=1 tests/integration/run_all.sh` runs it only after every
   ordinary lane finishes. Each app has a6hex ownership record under
   `spikes/dev-vpc-v1/build/p6/<run>/`; URLs/IDs use JSON outputs/state/receipts.
3. Wait for final pytest summary and teardown, including Lambda ENI release.
   Failed runs retain exact owner/log/recovery records. Use production
   `stlv tunnel recover` with the recorded bucket/home/target/app/env/session;
   never sweep unrelated integration owners or steal a live creator claim.
4. Run `p6_finalize.py <run...> --purge` via repository Python/shared AWS
   assertion session. It certifies app/session effects are absent before
   removing exact empty app metadata/passphrase; unresolved owners stay retained.
   Record every A01–A14 result and historical proof dependencies in this task.
5. From the second matching venv, use normal `stlv tunnel cleanup`; verify
   helper/plist/socket/state/owned resolver files gone and unrelated baseline
   preserved. Repeat cleanup for idempotence. No final green until all effects
   and required assertions are certified, including failed earlier owners.

Historical run session11909 used four reviewed cases, final3PASS/1FAIL above.
Its helper was freshly installed by clean
wheel venv C (now moved to /private/tmp/stlv-p6-retired-wheel-installer); runtime
is example venv B. Installed Python source/assets matched the checkout. Latest
standalone native OS DNS counterexample PASS; final fixture fixes are included
in that four-case run. No helper change while a live fixture remains active.

### P5 complete — 2026-10-07

- Added CLI --network-mode auto selection (other modes rejected by Click before
  deployment). First wiring captures resolved deployment state, provider profiles,
  immutable AWS contexts and home bucket/account/region before CommandRun exits.
- New tunnel/session.py controls runtime status snapshots, per-endpoint all-VPC
  admission, disabled/non-VPC bypass and cleanup. Bridge rejects before dispatch;
  manual event-loop teardown avoids joining an uncooperative executor handler
  before network cleanup. Existing serial handler execution remains.
- Production `stlv tunnel recover` now uses exact app/env/session prefixes,
  home/target account and UID fences, refuses live owners, stops registered stale
  actors, resumes metadata-only disposal, and aggregates residual units. Missing
  claims with current or version-hidden creation/actor evidence fail closed.
  Unknown SSM start outcomes retain ownership rather than falsely certify cleanup.
- Session diagnostics print residual VPC identities and exact host/AWS recovery
  commands. Runtime shutdown retries every access owner, including partial startup
  with no worker. Expired queued messages reject before import/handler execution.
- Final validation: 725 CLI/bridge/tunnel/Function tests PASS (session9286), Ruff
  and staged/unstaged whitespace checks clear. Sequential source/test reviews
  have no blockers. Acted on provider override validation, live/version-hidden
  missing-claim evidence, retained-unit failure exit, invocation deadlines and
  global-startup failure tests. No deferred review findings.
- Wheel/sdist built in /private/tmp/stlv-p5-release-dist. Clean installed wheel
  from /private/tmp with -I: actual CLI help/default auto and invalid early
  selection, recovery entrypoint, non-VPC bypass, both validated assets and real
  nonroot carrier startup/shutdown PASS. Wheel Python sources exactly match tree.
  Package proof /private/tmp/stlv-p5-package-check.py; venv /private/tmp/stlv-p4-wheel-check.
- G5 CLI/admission/lifecycle gate complete. This is not the full P6 live
  stlv dev → AWS URL → local handler → DocumentDB acceptance claim. No AWS
  resources created in P5; production helper/plist remain uninstalled. Original
  SDK chain/profile supported; provider-only auth/endpoint overrides explicitly
  refuse startup rather than substitute another credential source.
  User staging preserved; agent did not stage/commit/push or change branches.


### P4 complete — 2026-10-07

- Delivered protected startup credential/provider context, separate nonroot SDK
  runtime, independent SSH-over-SSM/EIC supervision, packaged Go carrier adapter,
  atomic DocumentDB cluster/reader/member discovery, scoped UDP/TCP DNS, bounded
  readiness/backoff, terminal per-VPC retirement and owned DNS rejection.
- Final validation: 541 tunnel tests PASS; Ruff clear; sequential source/test
  reviews have no blockers. Wheel/sdist match final reviewed source and both
  native asset manifests. Clean installed wheel from /private/tmp with -I imports
  runtime/SSM modules, validates both assets and starts real nonroot forwarder.
- Live networking evidence: two-VPC verified-TLS PyMongo member discovery and
  distinct writes/reads; interrupted first SSM transport recovered automatically
  while second driver remained usable. Separate fresh-name DNS loss/recovery and
  terminal changed-host-key proof PASS with healthy second-VPC TCP/other DNS.
  Named default profile and captured startup-environment credentials with invalid
  parent handler credentials/profile PASS. Core environment proof bdb81 exit0
  after idle same-attempt READY; resource_probe remains explicitly unverified
  when no resource is declared. Positive PHZ diagnostics are separate from that
  credential predicate; earlier failed whole runners are not recast as PASS.
- Lost successful CONFIGURE reply: installed-native production retirement proof
  PASS, exact request reconciled while DNS rejected, own routes removed/owned
  rejection retained, other ACTIVE unit/private DNS preserved; certain/empty
  final disposal. Unfenced/stale-generation attempts refuse publication/replay.
- Final read-only AWS audit PASS: 11 proof owners, 14 access units, 14 zones,
  4 DocumentDB clusters and 2 ordinary-CLI policy batches; no billable resources,
  owned SGs/interfaces/volumes, IAM roles/profiles/documents, active owned SSM
  sessions, passphrases or owner-prefix object versions/delete markers remain.
  Shared home bucket/bootstrap preserved. Script ignored build/p4-final-audit.py.
- Production helper uninstall PASS. Binary/plist/state/socket/bootstrap/retired
  artifacts absent; resolver directory empty; both proof CIDRs use normal en0
  default routes; no helper/forwarder/plugin/provider processes remain. Shared
  root0755 parent inodes69524940/69561207 preserved. Host gateway changed from
  192.168.178.1 to192.168.68.1 during proof work; the one host configuration
  refusal remains undiagnosed, followed by three successful local cycles and
  successful credential retry. Include host-network-change stress in P6.
- No active proof sessions/resources remain. Branch preserved; agent did not
  stage/commit/push. P5/P6 must prove the actual stlv dev app workflow; P4 completion
  does not claim the example already works through the CLI.

Earlier proof work and intermediate findings follow.


- Final lost-ACK proof also PASS with confirmed-transport-fencing guard, owner
  `27507c6a-2827-4c16-8442-1e1dbf6aa513`, exit0. Source review no blockers.
  Exact-request replay and prior DNS rejection assertions added; negative failed
  transport-fencing/stale-generation requests never replay/publish/retain falsely.
  Final test review and full suite pending. All live AWS proof runs exited and
  disposed; final audit session42735 checks11 proof owners plus2 policy batches.
  Native proof helper remains installed, certain/empty, pending uninstall.

- Credential ownerbdb81 PASS after poisoned parent and idle same-attempt READY;
  complete cleanup exit0 session53946. No AWS proofs remain active.
- Addressed review edge: save host request before RPC; terminal retirement inspects
  exact unit/generation and reconciles unknown CONFIGURE completion while DNS is
  rejecting, then removes only its routes retaining actual owned rejection. ACKed
  requests do not reactivate grants; unknown requests replay only after confirmed
  transport/forwarding fencing. Lease inspections serialize with mutations.
- Actual installed-native lost-success-reply proof PASS, session
  `00a014b1-d3ec-40c8-8bd6-6c9697b88c07`: first CONFIGURE committed but client
  reported UNCERTAIN; exact production retirement reconciled it, gen2 route removal
  kept queried DNS rejection, other ACTIVE unit/private marker survived; final
  host disposal certain/empty. Script build/p4-lost-host-reply.py, session63797.
  This proves lost ACTIVE ACK; genuinely partial PREPARING publication remains
  covered by native invariants/contracts, not falsely claimed injected live here.
- Final source/test review and full suite/build reruns after retirement fix pending;
  final exact-owner AWS audit and production helper uninstall remain.

- Credentialc56 was refused at host configuration; runtime normal EOF and all
  AWS fixture/access cleanup completed, exit1. Root independently certain/empty.
  Same unit/range and equivalent unique domain passed3 no-AWS configure/dispose
  cycles. Refusal not reproduced; added typed token-free native status causes
  (host-invalid/uncertain/unauthorized/protocol) for diagnosis, without weakening
  fail-closed behavior. Final retry owner `bdb81d2c-1c60-4284-9634-0dfc7a3a6bee`.
  Active session53946, default credential-only proof then full cleanup/audit/uninstall.

- Environmenta2f cleaned. Exact owned AWS-node TCP DNS returned the same public
  authority as host at first; later fresh raw query reached correct private answer,
  but legacy gethostbyname failed. This is not evidence of carrier misdemux;
  no product workaround. Credential proof now records post-poison READY+idle
  attempt stability before optional positive-PHZ diagnostics; diagnostics use
  ordinary getaddrinfo consistently with production/PyMongo and network proof.
  Final credential owner `c56dd01e-466d-483f-8909-4999a0bdb9b4`; active session79988.
  All earlier owners disposed; final audit of8 owners/11 access units/11 zones/4
  DocDB clusters/2 policy batches PASS, zero billable resources or owner versions.

- Environment68a cleaned; isolated poisoned-parent child READY, but marker failed
  with public SOA despite correct private records/association and INSYNC. Cannot
  infer root cause. Next owner `a2f194e0-d713-49a4-b14e-2e682da07be9` has fresh
  wildcard queries, bounded publication wait and direct owned-AWS-node TCP DNS
  diagnostic to distinguish VPC upstream from local routing. Recovery same script.
  Active session17329; helper remains installed only for these proofs.

- Owner905b network proof PASS and fully cleaned, both Fixture.down confirmed
  absence, runtime.close successful, session78952 exit0. Environment68a active
  session53564, same run/recover script command with its UUID. Source/test reviews
  clear; full537 tunnel tests PASS, Ruff clear. Clean installed-wheel imports and
  real packaged nonroot forwarder startup PASS from /private/tmp with -I.
  Pending: environment acceptance, final exact-owner cleanup audit, helper uninstall.

- Network-fault owner905b PASS: distinct two-VPC ordinary TCP markers; fresh
  private DNS outage produced no answers, unrelated iana.org and other-VPC TCP
  stayed usable; attempt2 restored private DNS/TCP; changed own SSH host key
  caused terminal identity-or-configuration, retained private rejection and
  preserved other VPC. Final cleanup underway session78952.
- Source and test reviews clear after real worker lifecycle/rejection coverage;
  21 targeted tests PASS, full previous535PASS. Wheel/sdist build and exact
  runtime/Go/source plus both asset manifests coherent. Next environment proof
  owner `68a13673-7b78-4da0-81a6-38656b9c32eb` uses a private example.com
  subdomain and waits Route53 INSYNC, with authority/RRset diagnostics if needed.
  Start only after owner905b host/AWS teardown; helper uninstall follows.

- Final network fault proof active, owner `905b1f65-03a7-4c21-b0ab-b56efd3eaa21`,
  session78952. Disposable fixtures `6fbd0b6f-6859-485f-97db-441e725aaf8a` and
  `8013791a-db72-4ed3-b635-35eb9ae63aa3`; VPCs vpc-003a947d0f1b3fb2b /
  vpc-094e3c6fa1fe2b013. Recovery: p4_network_fault_proof.py recover --owner <owner>.
  Environment4fcc cleaned completely; marker NXDOMAIN remains unaccepted.
  Full tunnel tests535PASS; final source review clear, phase reporting corrected.
  Test review pending. Helper remains installed only for these proofs.

- G2 remaining normal-CLI policy proof PASS, owneraecb15d9-c252-4121-a333-18755c0bf5fb:
  batches0/1 normal stlv deploy/diff/destroy, omitted/True/False and
  BastionConfig/dict/emptydict. Persistent instance counts1/3, no inbound SSH,
  no temporary-session tags during ordinarydeploy, persistent IDs stable and
  diff summarieszerochanges. All application VPCs/instances disposed; dedicated
  appstate/update/snapshot/lock/eventlog versions and SSM passphrases removed.
  Baseline sharedhome preserved; evidence ignored build/policy/<owner>/0,1/.
- Environment-source fixtures2f8d and51ca are cleaned; no readiness/marker
  acceptance inferred. New4fccb6ce-f855-4890-bc8f-5686e25b5094 captures
  token-free credential method and status phases. Runtime phase/initial600s
  startup budget, true host DNS payload and known missing-resource terminal
  classifications added; finalsource/testreviews pending.

- Real macOS fresh-name DNS outage proof PASS via installed native helper and
  bounded nonroot DNS fixture: private10.254.9.19, actual queried SERVFAIL
  yielded no addresses, recovered private answer, public1.1.1.1 baseline restored
  after cleanup. This excludes public fallback independently of earlier cached
  answer assertion. Evidence build/p4-dns-fallback.json. Initial10.252 range
  refused as uncertain; normal lease close and reconcile yielded certain/empty
  state; same proof on proven10.254 range passed. No AWS effects.
- AWS DNS prerequisites are now checked explicitly before host/access creation;
  both enableDnsSupport and enableDnsHostnames required true. Source HTTP
  transport failures retry, not terminal generic errors. Environment fixtures
 148131 and51ca cleaned; marker still NXDOMAIN despite explicit attributes,
  so new fixture2f8d3d8e-5f8b-4b4c-9dd4-c174c8bbcdef includes direct host DNS
  and PHZ association diagnostics. P4/G4 remains open.

- Fresh8da fixture/access/host teardown completed successfully; fault proof FAIL
  at public-canary OS lookup during owning private DNS loss: a returned address
  must now be distinguished as cached private vs public fallback. Full named-profile
  two-VPC connectivity and automatic reconnection passed. No live resources
  from8eb/8da retained. Environment-only148131 proof still active; G4 open.

- Initial8eb fixture completely removed after recovery (SDK final VPC absence).
  Fresh8da proof: both READY, distinct ordinary OS DNS and TLS driver writes/reads;
  first SSM session interrupted, second driver remained usable, first recovered
  automatically at attempt2 and both verified TLS reads passed. Subsequent fault
  proof currently cleaning up; original failure result pending. Host authority
  already certain/empty. A fresh environment-source fixture
  `148131c5-ace6-4be3-b2b9-19381ee81d6d` runs independently with no known resource
  (explicit unverified resource status), true host TCP DNS payload and parent
  handler credential/profile mutation; dedicated fixture/access records persisted.

- Current proof identities: initial/retry fixture `8ebf5900-131f-4ad0-8898-6532373e8dec`
  access stacks recovered and metadata removed; DB/zone/VPC teardown running.
  Retry fixture mistakenly reused its runtime UUID while AWS retained terminated
  instance tags, correctly rejected as ambiguous. No production ownership
  checks weakened. Two exact initial terminated IDs i-06aa640912d0a4c20
  (unit0ccc878a), i-00a05e24ab8ba5ff3(unit0752c48b) had only their verified
  proof tags retired. New fixture `8da488cb-f694-4b73-bce2-6ac1c49af49f`
  uses fresh runtime session UUID, recorded separately in fixture.json.
- Native helper certain/empty before new proof. Normal shutdown now closes
  EOF lease immediately despite an in-flight mutation, then certifies empty
  host ownership separately; AWS cleanup gets bounded reconciliation time.
  Source review clear; full tunnel529 PASS; wheel/sdist assets verified.

- Live initial proof: both production VPC workers READY; macOS private DNS
  markers distinct; real PyMongo TLS/hostname checks, rs0 member discovery and
  separate write/read markers PASS. Healthy second DB still read after first
  SSM termination. Reconnection/cleanup initially FAIL: externally ended SSM
  session returned ValidationException; Darwin libproc stops reporting zombies
  before reap. Fixed with exact terminated SSM history ownership verification
  and native waitid(WNOWAIT) child fencing. Real child, stale birth, concurrent
  startup/stop and TERM-ignoring descendant tests pass (17 targeted cases).
  Failed attempt access recovered; same fixture resume in progress. No G4 claim.

- Latest continuation: production isolated runtime, authenticated/pinned SSM+EIC
  SSH, packaged Go carrier stacks, readiness, discovery refresh and per-VPC
  recovery implemented. Source review cleared after atomic actor admission and
  SSH birth-registration fencing fixes. Full tunnel suite521 PASS; Go race test
  PASS. Significant-test review requires guaranteed fixture cleanup (being fixed).
- Live proof owner `8ebf5900-131f-4ad0-8898-6532373e8dec`, default profile,
  account535368238919/us-east-1; fixture intent/evidence in ignored
  `spikes/dev-vpc-v1/build/p4/8ebf5900-131f-4ad0-8898-6532373e8dec/fixture.json`.
  Two disposable VPCs, private zones, DocDB clusters; production runtime creates
  independently owned temporary access. Run/recover via `p4_proof.py`.
  Helper installed through normal installer; new artifact SHA
  `6f2ef902d8615797cdbcc33deed6afcfd244dd6d27517d9ae29887b5636d8911`.
  AWS/host proofs and final cleanup not yet complete; G4 remains open.

- Completing P4: new production Go carrier demultiplexer/stacks and SSM/EIC
  transport under implementation (not yet reviewed/proved). Go dependencies
  pinned and checksummed independently, no prototype branch copied. Native
  helper sources unchanged. Need runtime/supervisor wiring, tests, release asset
  manifest and G4 live proofs. No live AWS/host effects in this continuation yet.
- Added initial runtime/worker/access composition. Review found six concrete
  blockers; fixes now being tested: atomic registry revoke vs spawn/ACK,
  birth-fenced unreaped SSH group cleanup, bootstrap quiescence before worker
  cleanup, exit70 reported as incomplete ownership cleanup, serialized helper
  mutation requests, immutable SSM DocumentVersion+SHA256 on SendCommand.
  Runtime/adapters have not had live install/AWS proof yet. Existing full tunnel
  regression502 PASS before these review fixes; native append transition7 new
  cases plus helper/actors targeted147 PASS afterward. Go carrier test is pending
  rerun after allowing ordinary fixture SYN retransmissions. Do not claim G4.

- Added `credentials.py`: capture provider/account/region/profile and startup
  environment before handlers mutate it. SDK sessions may open only in a separate
  nonroot process, verify account, retain the SDK provider and refresh callbacks.
  The actual product runtime launcher remains to implement.
- Added `discovery.py`: exact cluster/reader/member hostnames, account/region/VPC,
  engine, ports and security groups verified through specific DocumentDB APIs.
  Recheck membership/configuration before publishing; no IP cache or regional
  suffix ownership. Publish atomically with per-VPC generation fencing and
  cross-owner conflict validation, preserving other VPCs' snapshots.
- Added `socks.py`: numeric IPv4 destinations restricted to one VPC's ranges,
  loopback proxy, nonroot enforcement, a bounded handshake deadline, exact
  nonblocking IO, cancellation and failure closure. Authenticating the SSH proxy
  belongs to the pending transport supervisor, not this TCP adapter.
- Added `dns.py`: scoped custom domains and exact resource hosts, one exclusive
  loopback UDP/TCP port, bounded request concurrency/client lifetime, private
  resolver over SOCKS TCP only. Preserve aliases, TTLs and negative answers;
  truncate large UDP answers for ordinary TCP fallback. Outage retains rejection,
  stale generation cannot publish/revive an answer; never choose public DNS.
  Added dnspython>=2.8,<3 and locked2.8.0. `uv sync --locked` removed three
  previously untracked spike-only venv packages (awslambdaric/simplejson/
  snapshot-restore-py); project lock remains authoritative.
- Checks: full tunnel suite502 PASS (79 existing warnings), including51 new
  P4 cases. Real child interpreters select captured environment/named-profile
  SDK sources after parent handler mutations; account drift, foreign session
  and child mutation refuse. SDK AWS API calls are stubbed. Real local TCP/DNS
  peers prove two distinct owning resolver paths, one-view outage with the other
  usable, label/exact-host boundaries, alias/TTL/NXDOMAIN, truncation→TCP retry,
  late-view rejection and recovery, post-handshake partial EOF/cancellation.
  Source and significant-test reviews clear. Ruff format/check PASS.
  Socket tests require unsandboxed nonroot loopback binds; no root/host/AWS effects.
- Next: isolated product runtime, authenticated SSH-over-SSM/EIC transport,
  Go carrier and independent recovery/readiness, wiring these tested adapters.
  G4 is open; P5 CLI integration is required before the example can work.

### P3/G3 completed — 2026-10-07

- Implemented `native/admin.c/.h`, `installation.py` and `cli/tunnel.py`:
  `stlv tunnel install`, `inspect`, `reconcile`, `cleanup`. The bootstrap elevates
  only fixed system tools and the exact SHA256-checked image at the installed
  path. No privileged project/venv Python, arbitrary executable paths or AWS.
- Native install receipts are durable image xattrs tying image/directory/plist/
  socket inodes to installation intent. Reserved staging-directory intent is
  persisted before mkdir; plist receipt precedes exclusive public publication.
  Canonical private prefixes recover partial writes. Every admin mutation
  refreshes receipts under exclusion. Shared-parent baseline is preserved.
- A root0600 admission file uses a shared flock throughout each native session
  and cleanup, exclusive throughout install/uninstall. Active uninstall refuses
  before bootout. New acquisitions cannot race uninstall. Plist/socket/image/
  directory disposal uses exclusive quarantine and actual moved-inode checks;
  interruption preserves ownership and normal retry paths.
- Idempotent matching installs require no elevation, including authenticated
  BUSY replies from an active compatible service. Different packaged artifacts
  refuse before elevation and require matching-package cleanup first. Offline
  uninstall is separated from exact activation-profile checks, so an OS update
  does not disable owned recovery. Installation readiness has one-second probes.
- Live-found production fixes: Darwin AF_UNIX rejects SO_ACCEPTCONN with
  ENOPROTOOPT; validate the launchd-bound root socket and idempotently listen
  instead. Every accepted control connection resets its slot, including after
  acquisition handoff and rejected traffic. Reject stale/mismatched mutations
  before disabling healthy packet grants. Fixed public startup exit codes10–17
  diagnose boundaries without exposing capabilities/journal data.
- Source and significant-test reviews cleared. Final regression: tunnel451 PASS,
  CLI47 PASS (79 existing warnings in each); relevant Ruff format/check PASS.
  Native strict C17/O2/Wall/Wextra/Werror build PASS; real wheel and sdist include
  native asset, coherent manifest and sources. User-created commits include
  `0207d0d1`/`e2dd0315`; preserve the user's index. No agent commit/push performed.
- Final artifact SHA256:
  `0c32775b92d36f2d7502ce67257d0a87be3ea72f9d13abc38e0f10a2be9763ec`.
  Source fingerprint:
  `d73541afd17d2f201b674595df293e3cbb6f62ecd4700605678f3e1c1c918866`.
  Direct closure: libSystem, libbsm, SystemConfiguration, CoreFoundation.
  Live profile: macOS15.7.5/Darwin24.6.0 arm64, Python3.12.11.
- A01 PASS: install final wheel in `/private/tmp/stlv-p3-venv-a`, delete A, use
  independent installed wheel B without reinstall. Actual OS getaddrinfo routes
  two unique private .invalid domains to independent nonroot DNS fixtures.
  Real ordinary host TCP SYN/connection/data traverses root utun -> nonroot
  carrier -> root utun. Removing the first unit leaves the second TCP path usable.
  Carrier v1 includes unit32/gen64 plus the four-byte network-order AF_INET
  prefix and IP bytes; P4 must preserve that framing.
- A09 PASS: native active uninstall refuses without disrupting the owner;
  uninstall from B, complete dedicated-artifact removal, repeated cleanup,
  interrupted hard-link publication (verified root same-inode nlink2), recovery
  through normal install, fresh acquisition/use, reinstall and final uninstall.
- Helper A10/A11 PASS: kernel-authenticated second PID refused; stale generation,
  bad capability and overlapping VPC grant refused, exact two healthy grants and
  subsequent TCP preserved; incoming SCM_RIGHTS rejected; actual root control
  caller (fixed system nc/printf) rejected, nonroot inspection remains healthy.
  Deterministic ABI/asset incompatibility negatives pass client/install tests.
  Full transport/IAM/security acceptance remains with P2/P4/P6.
- Recovery PASS: SIGKILL only our nonroot owner, retaining a duplicated carrier
  in another process, removes the actual kernel interface and root state. Native
  daemon SIGKILL with deliberately incomplete `journal.next` recovers typed
  journal and resolver inode receipts; same-owner reacquire gets a new capability,
  empty/certain inventory and no old kernel interface before fresh configure.
  Old capability is rejected; final lease cleanup removes the new records.
- Proof fixtures: `spikes/dev-vpc-v1/p3_proof.py`, `p3_stale_proof.py`,
  `p3_restart_proof.py`, `p3_install_recovery_proof.py`. Actual result JSONs and
  physical baseline/final cleanup snapshots are in ignored `build/p3-*.json`.
  Main final session938d5183-1b6d-4722-9dbd-4d7a19719a5f; daemon recovery
  session417fb147-0066-4a18-a809-2640d09ab3fa. Initial proof failures were fixed
  and rerun; one recovery-fixture setup returned uncertain immediately after the
  preceding teardown, then identical config was independently verified and the
  complete recovery rerun passed. No general transport readiness claim follows.
- Final independently verified absent: installed image, LaunchDaemon plist,
  launchd label, dedicated state/socket/journal, digest-qualified bootstrap files
  and retired/staging directories. All baseline interfaces and resolver entries
  match. Independent route lookups for both VPC destinations and all four
  documentation interface addresses return the original default viaen0; both
  proof venvs are deleted, wheel/sdist and ignored evidence retained.
  Shared Stelvio parent inode69524940 and resolver dir inode69561207 with
  original root0755 modes remain. No AWS resources created by P3 or AWS records
  removed by uninstall. Existing root Python PID17812 was never targeted; final
  read-only inventory showed it absent (do not infer our cleanup removed it).

### Earlier implementation evidence

- Latest full tunnel regression444 PASS,74 warnings outside sandbox. Source and
  test reviews cleared service/client/ACL/incremental IO/assets findings. Helper
  client31 tests include full64-bit per-unit inspection, compatibility negatives,
  finite fixture sockets and complete received-FD cleanup. Native IO86 cases;
  actual POSIX-private/inherited ACL4 cases; native asset18 cases use independent
  golden source digest rather than reader-generated expectations.
- Release builder captures immutable C/header inputs before compilation, checks
  live source digest before publication, enforces nonroot execution and fixed
  system tool/SDK/library inventory. Generated helper-macos-arm64 and manifest
  included in real wheel/sdist; archive hash and source/client contents checked.
  Isolated wheel imports, complete source fingerprint validation, native version
  and nonroot --serve refusal PASS. No installer/project interpreter required
  for native version; actual cross-venv installed-helper proof remains NOT RUN.
- Service public inspection now reports bounded per-unit32/gen64/status entries
  from main-owned caches after worker join, including persistent failed flags.
  Startup caches recovered metadata without adopting saved kernel indices.
  One nonblocking reply avoids main-loop backpressure stalls. All root kernel
  descriptors close before any cleanup journal read; unreadable cache revokes
  lease, retains metadata and reports uncertainty. Dead-owner explicit recovery
  is reachable; monotonic frame budgets reread before receive/dispatch.
- P3 host journal/staging and socket now live at persistent fixed path
  `/Library/Application Support/Stelvio/tunnel`, with root0711 directory and
  root0600 secret records. Parent inode/ACL checks and fsync precede effects.
  Shared `/Library/Application Support/Stelvio` root0755 is pre-existing baseline
  and must be preserved. Extended ACLs are refused, never rewritten. Native
  artifact, persistent leaf and LaunchDaemon plist independently absent after
  these nonroot checks; no installation/routes/DNS/AWS changes run this turn.
  Next concrete installer design: fixed trusted native admin operations; durable
  installation ownership receipts before exclusive LaunchDaemon publication;
  active refusal and inode-fenced cleanup/recovery; system-only bootstrap copies
  the hash-checked asset to fixed trusted path. Never elevate project Python.

- First native service implementation (`service.h/.c`, `main.c`) compiles with
  strict flags. launchd socket activation, bounded8 incremental control streams,
  peer/session/capability checks, EOF lease, independent packet grant copies and
  one native mutation worker compose existing unit transactions. Root kernel FDs
  are never delegated. Replies use one nonblocking send; foreign live-owner
  callers refused before frame reads. Source-review fixes: dead-owner explicit
  recovery admission, fresh frame deadlines, revoke direct FDs despite unreadable
  journals, cache uncertainty and persistent failed-unit grants. Final review
  pending; not installed/live-tested; no service acceptance claim yet.
- Recovery namespace moved to persistent `/Library/Application Support/Stelvio/
  tunnel` (0711, root-only listing/writes; private records0600) so reboot cannot
  erase ownership while resolver files survive. Socket uses that namespace too.
  Existing root-owned0755 shared Stelvio parent is baseline, never delete it.
  Parent-directory inode fencing+fsync precedes journal/host effects; native ACL
  checks reject extended entries on trusted paths/FDs rather than rewriting
  another tool's permissions. Actual user-fixture tests cover POSIX-private
  inherited ACLs; final source/test review pending. No privileged changes run.
- New nonroot `helper_client.py` provides bounded reply/descriptor checks,
  root-server Unix peer verification, ABI/carrier checks, same-process EOF lease,
  configure/remove/release/reconcile. Capabilities excluded from repr/errors;
  acquired descriptors noninheritable; platform APIs loaded lazily. Ruff passes;
  source/tests/install proof pending. Installer/package assets still absent.

- Latest combined native regression263 PASS,74 warnings outside sandbox (the
  sandbox-only Unix-socket bind failure resolved in real nonroot rerun). Four
  additional final-disposal successor cases subsequently pass in the105-case
  snapshot suite. Ruff checks pass. All native product sources link together;
  rebuilt nonroot closure inspection includes only libbsm, SystemConfiguration,
  CoreFoundation and libSystem direct imports; --version passes. This is not an
  installed-root/helper service proof. Trusted artifact/state paths independently
  absent and pre-existing resolver directory unchanged; no new AWS created.

- Native unit transactions (`unit.h/.c`) now compose fresh sole-root utun,
  committed PREPARING, exclusive routes/resolver receipts, and ACTIVE host config;
  removal commits durable intent before detach/DNS effects. Reviewer findings
  addressed: `STLVSNP3` keep_dns field frozen within REMOVING; same-generation
  retry cannot switch policy; published inode/content and actual routes checked
  before ACTIVE; retained receipts checked; checked close and bounded3s read-only
  interface/exact-route disappearance before terminal commit. Cached indices
  still authorize no mutations/adoption. Strict compiler passes; final source/
  transition-test reviews cleared reported findings. Not wired into a native
  service or live-proved. A final narrow recovery addition allows strictly higher
  generation nonretaining disposal to abandon unfinished retaining REMOVING
  (unchanged receipts/configuration); same-generation intent remains frozen.
  Final source/test reviews of that addition clear.
- Foreign DNS inventory now covers bounded resolver(5) files and native macOS
  SystemConfiguration domain/supplemental-match entries; label-boundary overlaps,
  ambiguous files, symlinks and unreadable inventories refuse. Familiar own
  filenames excluded only with matching committed inode/content receipts.
  Source review clear. Parser30/snapshot96/journal32 targeted cases158 PASS,
  74 warnings; size/retention test review clear. Nonroot read-only actual
  SystemConfiguration reserved.invalid proof PASS outside sandbox; no settings
  changed. This does not prove effective OS DNS readiness or complete runtime
  installation. Native closure adds SystemConfiguration/CoreFoundation frameworks
  and system transitive dependencies; previous detach artifact closure is not
  sufficient evidence for the eventual full helper. No AWS/root artifacts added.

- Resolver ownership slice implemented and source-reviewed: session/unit/config-
  generation filenames with explicit DNS domain; exclusive hard-link publication,
  inode/content receipts before publication, atomic quarantine and foreign-entry
  restoration on removal. Unreceipted private writes resume only root single-link
  canonical prefixes at the committed capability-derived reserved name. ACTIVE/
  RETAINED current checks reread committed state and actual public inode/content;
  these do not establish OS DNS readiness. Native strict compiler passes.
- Incomplete journal.next recovery now recognizes physically incomplete framing
  prefixes inside the locked root namespace, validates prior typed state/absence,
  fences the actual inode, unlinks and fsyncs; complete malformed snapshots remain.
  Journal files require exact0600. Source review clear; new pure classifier and
  formatter checks115 PASS (83 snapshot/formatter +32 classifier),74 warnings.
  New test review pending. No root execution/installation/DNS/AWS effects in this
  slice. Remaining: foreign DNS inventory, native unit/service/installer wiring,
  actual journal/resolver crash and DNS proofs, transport and CLI integration.

- P3 packet carrier/pump primitives now exist (`packet_io.h/.c`, `pump.h/.c`):
  nonblocking bounded Unix datagrams carry unit32/generation64 and exact packet
  bytes; one finite datagram per pump call; root kernel FD never delegated.
  Shared ancillary closer handles all actual copied descriptors even in empty
  datagrams. Real native tests cover 0/1/33/254 rights, full FD counts/sentinels,
  exact high+low 64-bit generation and bytes, zero identity and short-frame
  rejection. Carrier suite76 PASS; TCP/IPv4 grant suite24 PASS includes both
  CIDRs in both directions. Interface sockets now nonblocking/noSIGPIPE.
  Source review cleared primitives, test review gaps resolved. Pump still has no
  service/live forwarding wiring, so no packet-flow or G3 acceptance claim.
- Typed snapshot evolved to `STLVSNP2`: fixed carrier version1 and separate
  per-unit kernel interface index, diagnostic only after FD loss. Index remains
  fenced during a unit lifetime; removed→higher-generation preparing may use a
  fresh index. Tests75 PASS include distinct per-unit values, live changed-index
  refusal, fresh-lifetime changed-index acceptance, legacy/version/index bounds.
  Source/test reviews cleared. Service must enforce distinct actual live FDs,
  not infer ownership or numerical uniqueness from historical indices. Last
  combined check171 PASS before V2's four extra schema cases; individual latest
  suites imply175 cases, but that combined command has not yet been rerun.
  Root detach proof evidence saved in ignored build/detach-proof-result.json.
  No proof actors, new AWS resources, resolver changes or installation remain;
  existing empty `/private/etc/resolver` directory is preserved as baseline.

- 2026-10-07 resumed interrupted check: targeted native suites 154 PASS (65 IO/
  peer/protocol,71 snapshots,18 packet); no interrupted proof actors remained.
  Preserved pre-existing root example Python PID17812. Native source reviews
  cleared fixed detach proof. macOS administrator authorization ran only the
  hash-checked native binary from trusted installed path, fixed system bootstrap,
  exclusive publication and inode-fenced removal. Session4004 exit0: two-unit
  interface detach PASS, retained duplicate Unix carrier FD cannot retain utun;
  first kernel interface/route absent while second remained live/owned, then
  second absent. Installed proof/temp artifact independently absent; no state,
  resolver or AWS created. This is teardown mechanism evidence, not G3 service
  or completed packet-pump acceptance. Packet policy now excludes IPv4 options
  and non-TCP protocols (TCP options untouched); 22 packet tests PASS, final
  review pending. Native service/CLI/full example still unimplemented.

- P3 native route review found unscoped RTM_DELETE's key is only destination/
  mask in selected XNU, so interface/gateway preflight cannot prevent deleting
  a concurrent foreign replacement. Removed that operation entirely: adapter
  only adds exclusive prefixes. Scoped-only routes do not serve ordinary unbound
  host sockets; shared delegated utun FDs cannot prove cleanup on helper close.
  Candidate refinement: helper retains sole kernel utun FD per VPC; nonroot
  forwarding gets a revocable Unix packet endpoint. Kernel detach should remove
  only that VPC's interface-attached routes. `packet.h/.c` independently bounds
  IPv4/AF framing, total length and grant/direction; read-only tests 18 PASS.
  `check_detach.c` is a concrete fixed two-unit native mechanism proof, compiled
  but not yet root-installed/run. It retains a duplicate Unix packet descriptor,
  closes first kernel FD and checks other unit remains, then checks both absent.
  No AWS/resolver/service/lease changes. Source review pending; live elevation
  still required. Current product artifact and state paths are absent.

- P3 continuation adds `snapshot.h/.c`: explicit-endian typed recovery state,
  audit/session/capability consistency, per-unit phase/generation, canonical
  native-decoded configuration and resolver inode receipts; cross-unit CIDR/DNS
  ownership checks. `snapshot_successor` fences immutable session/peer/interface,
  exact revision increment, one unit per write, receipt phases and tombstone
  generation reuse. Read-only harness tests 66 PASS, including nonzero identity
  mismatches and destination prefill/clearing. Source codec review clear; latest
  transitions/harness/test review pending. New `state_store.h/.c` ties the typed
  validator to journal read/save/complete-pending promotion. Review found retry
  after successful rename/failed directory fsync; fixed no-pending recovery to
  fsync the directory too. Incomplete/malformed pending snapshots remain error/
  retained, requiring further recovery implementation; no full G3 claim.
- New `interface.h/.c` creates a fresh kernel utun, verifies its live descriptor/
  name/index, configures fixed documentation-only point-to-point addresses and
  adds/removes validated VPC routes through bounded native routing-socket IO.
  Changes require the trusted installed root image; cached journal indices never
  authorize deletion. Strict compiler PASS; review/live validation pending. No
  service calls or privileged host effects have run.
- IO review gaps fixed: Darwin receive-queue consumption synchronizes incomplete
  first fragment before the remainder; CONFIGURE body descriptor injection also
  rejects without leaks. Actual peer/framing suite 65 PASS. No AWS resources,
  helper installation, routes or resolver changes created in this continuation.

- P3 peer source/test reviews cleared, actual peer boundary PASS. New native
  journal primitive (`journal.h/.c`) has root/loaded-image/lease/dir-inode
  fences, bounded complete snapshot checks, file+directory fsync, O_EXCL next
  snapshot, prior contents validation before replacement. Source-review fixes:
  nonblocking opens prevent FIFO hangs; absence retry fsyncs prior uncertain
  unlink. Primitive compiled/reviewed; typed service schema and `.next` crash
  recovery not yet implemented or live-tested, so no G3 claim.
- Native `io.h/.c` adds ten-second bounded one-frame receive/reply, max4096
  reply and one-time SCM_RIGHTS delivery. Source review found oversized incoming
  descriptor truncation could read outside buffer and leak already-installed FDs
  on Darwin. Fixed actual copied-byte bounds, complete FD-slot closure, aligned
  4096-byte buffer above the selected kernel's full control-mbuf bound. Actual
  nonroot native tests rejected 1/33/128/254 incoming FDs with equal before/after
  FD counts and live service sentinel descriptors; 255 rejected by kernel
  EINVAL, ordinary frame accepted. Segmented frame and truncated EOF pass.
  Native profile check enforces macOS15.7.5/Darwin24.6.0/arm64 before IO. Source
  limits verified against Apple xnu-11417.140.69 `sockargs`/`unp_internalize`;
  generic recv without control would not safely solve this on the selected
  kernel. Latest source/test review pending: 64 PASS, 74 warnings; strict clang
  and Ruff PASS. No installation/root networking/AWS resources created.

- P3 adds native `ownership.h/.c`: trusted installed path and actual loaded
  vnode generation, inode-fenced installation/state locks, kernel Unix peer
  UID/PID/birth. Source review caught the connect→PID-reuse→birth-capture race;
  fixed using `LOCAL_PEERTOKEN` and `proc_pidpath_audittoken` around lookup and
  liveness. Apple primary libsyscall implementation confirms PID-version
  comparison (`PIF_COMPARE_IDVERSION`); source:
  https://github.com/apple-oss-distributions/xnu/blob/main/libsyscall/wrappers/libproc/libproc.c.
  Root helper closure now includes system libbsm/libSystem; strict C compiler
  passes. Nonroot `check_peer.c` harness tests actual socket identity, stale birth,
  stale audit generation and uninstalled root image/lock refusal; test pending.
  Tunnel regression before this latest peer addition: 170 PASS, 74 warnings.
  Source/test reviews latest slice pending. No root artifact/service/networking
  or AWS resources created; remain on requested branch and preserve user index.

- P3 protocol slice: `helper_protocol.py`, native `protocol.h/.c` and read-only
  `check_request.c` compile with strict C17 flags. 56 compiled-native cases PASS:
  six operation-scoped positives pin exact generation/session/unit/VPC/CIDR
  network+mask/domain+port and retention values; fixed public-fixture capability
  comparison never outputs received capability. Direct raw malformed packets
  reject bounds/headers/path/default-route/overlap/identity and operation misuse.
  Test-review gaps on full decoded values and other operation scopes addressed.
  New read-only `host.h/.c` inspects macOS kernel routes, with distinct error,
  absent, owned and foreign outcomes and exact-interface route fencing; strict
  compiler PASS. Source/test review of latest slice pending. No active native
  service, installation, host networking or AWS resources created.

- Local provider-death rerun PASS (session30727 exit0): exact pinned RPC startup
  failure on a killed registered provider; no successful replacement preview.
  Existing preview + logical provider up/refresh/destroy passed, 12 fresh native
  pairs, all stopped. Scope is this injection point, not all imaginable failure
  modes. Pulumi emitted an ignored coroutine shutdown warning after the deliberate
  failed inline preview; no AWS resources, backend retained or actors remained.
  P3 started production `helper_protocol.py` and independent native C decoder
  under `stelvio/tunnel/native/`; fixed bounded binary requests contain only
  session capability, operation, generation, VPC ID, canonical private CIDRs
  and normalized DNS domains/unprivileged loopback ports. No root service or
  installation exists yet; no privilege/host changes. Strict C17 compiler PASS;
  first compiled native protocol contract test pending corrected fixture length.

- Actual kill-during-create/recovery PASS, owner
  `a4bc2999-49d7-4268-90fa-84641e3a427a` (session91023 exit0). Controller
  froze a live native `up` engine and AWS provider after owned group
  `sg-0cd9c3365cfebc4a5` appeared; stopped seven durable registered process
  identities (including creator) before fresh-process claim transfer. Production
  recovery reconciled incomplete creation, refreshed/destroyed its separate
  backend, removed every encrypted version/marker, key and owner record. Target
  workload ingress and independently owned fixture preserved until explicit
  fixture teardown; exact-ID/tag absence confirmed. No proof resources remain.
  Safe receipts now include operation plus allowlisted CLI verb; raw arguments
  never persisted. Reviewer privacy assertion added; actor tests 4 PASS.
  Local provider-death negative proof failed only its expected-error wording:
  actual pinned engine emitted `aws (resource) plugin [aws] did not begin
  responding to RPC connections`; proof assertion updated to this exact error,
  rerun pending. Its fake-credential local backend made no AWS resources.

- Live interrupted-create proof starting with new owner
  `a4bc2999-49d7-4268-90fa-84641e3a427a`, default/us-east-1/existing Home
  bucket, `aws_access_interrupt.py`. Source review cleared exact birth freezing,
  live native `up` gate and failure retention; actor tests 4 PASS. Controller
  recovers via a separate `aws_access_lifecycle.py recover` process after killing
  the creator/native actors. If interrupted, use that recovery command with this
  owner/bucket/region before moving on. Evidence/WAL in ignored build directory.

- Uninterrupted production P2 lifecycle PASS, default profile/us-east-1, owner
  `38321ac9-f086-41ec-91ce-770956914537` (session22926 exit0). Created temporary
  instance `i-0f5e1e96f322af148`, group `sg-04f68b7bd227d804f`, IAM/profile,
  SSM document/root volume/target ingress; actual SSM readiness PASS. Normal stop
  twice completed SDK cleanup, independent backend refresh/destroy, all S3
  versions/markers, generation-bound key and metadata removal. Original target
  ingress preserved; exact-ID and tagged fixture absence checked (including VPC
  `vpc-0cfa2320361eaf007`). Source/test review cleared terminated-instance
  context fencing, serialization roundtrip and uncertain key handling.
  No DocumentDB/helper/route/resolver changes. Interruption controller pending
  review: freeze exact creator before fallible discovery, record safe actor
  operation and native CLI verb, require a live `access-create`/`up` engine and
  provider, then fresh-process recovery. P3–P7 still unimplemented.

- P1/G1 complete: policy normalization, immutable multi-VPC contracts, private
  VPC/DocumentDB/bridge endpoint metadata, resolved full-CIDR/account/subnet SDK
  validation, and CLI-state reader. Customized DocumentDB metadata uses the actual
  cluster groups and port. Config/dataclass synchronization check added. Focused
  P1+VPC suite 104 passed; P1+Vpc+DocumentDB+Function suite 203 passed after initial
  P2 additions. Source/test reviews completed and reported gaps addressed in P1.
- P2 persistent work added shared `stelvio/tunnel/bastion.py` builder and lazy
  VPC creation for True/config/dictionary forms, including unused VPCs. SSM-only
  loopback SSH, dedicated nonroot user, encrypted/deleted disk, IMDSv2 and provider-
  bound normal Amazon AL2023 arm64 image. Persistent cluster access follows actual
  port and customized target SGs; ordinary workload ingress is preserved. Initial
  source/test review findings resolved: default/custom-list ingress registers
  synchronously, Output[list] fallback emits a preview warning, metadata names the
  component-owned source group (runtime must verify instance membership), and
  independent exact IAM/bootstrap/AMI/customization/tag expectations added.
  Persistent slice plus VPC/DocDB/Function/bridge: 296 passed, 86 warnings. 24
  component-metadata tests pass. G2 remains open; no new AWS/host changes.
- P2 temporary boundary now has `access_state.py` (immutable remotely conditional
  intent/claim/observed records; safe namespace encoding and idempotent lost-
  response claim release), `access_program.py` (independent access-only Pulumi
  program), `access_backend.py` (owner namespace and tagged encryption key),
  `access_inventory.py` (account/tag/ID/resource validation), `access_cleanup.py`
  (record IDs before SDK deletion, known-ID absence, partial retries and foreign
  dependency refusal), and `access_unit.py` (mutable owner with injected operation
  runner/stopped-engine proof; independent refresh/destroy). These are not yet
  called by the product CLI and have not been used against AWS.
  Immutable intent includes creation-time SSM document, IAM permission and boot
  contents so recovery does not depend on a newer package's recipe. Root disks
  can be observed via owned instance mappings if tagging is incomplete; exact
  recorded IDs are checked before deleting orphaned disks. SSM document identity
  includes creation date/hash/version, not just its reusable name. Profile cleanup
  refuses foreign instances/associations. Expiring AWS credentials no longer go
  into provider state: the backend supplies a freshly captured child environment
  per operation, while provider account/region stay explicit and profile variables
  are cleared. Home and target credentials are captured separately: the backend
  receives Home credentials, each fresh registered AWS provider receives target
  credentials with account/region validation. Latest tunnel suite excluding the
  older process-tree tests: 101 passed, 74 warnings (before latest EOF case).
  Checkpoint and pending physical AWS IDs now fence against SDK/saved inventories
  before refresh/destroy. Each engine refreshes workspace credentials; distinct
  saved recipes reconstruct from remote bytes. Source/test review cleared this
  boundary. Backend/key/metadata disposal is implemented: remove encrypted S3
  versions before the generation-bound key, keep a final metadata disposal receipt
  across partial deletion, reject new claims while it exists, resume certified
  disposal without reopening a deleted backend, and repeated stop is harmless.
  Key receipts include LastModifiedDate/version/hash; SSM region is pinned.
  New `processes.py` uses macOS libproc birth identity and parent-first freeze,
  persists all descendants before kill (including separate process groups), and
  fails closed on missing/reparented ancestors. Controlled local real-process
  proof passed outside the sandbox (one creator/two separate-group children;
  stale birth identity rejected); this is not yet wired to a production runner.
  New metadata/key/process changes still need final review and live G2 proof.
  Open before G2: recovery claim acquisition;
  complete process/engine/provider death proof; safe backend-version/recovery-key
  runner integration; remaining volume/backend tests;
  actual serial default-profile G2 proofs with complete cleanup. A pinned-provider
  source lookup to verify atomic volume tags failed due GitHub connectivity even
  with sandbox escalation; do not claim that verification. No new AWS resources,
  native installation, routes, resolver state or proof processes were created.
  P3–P7 remain unimplemented and the example is not yet working in dev mode.

- Supplemental process-ownership finding: post-hoc ancestry cannot establish
  absence of providers orphaned through unknown intermediate processes. The
  earlier controlled tree proof remains evidence only for that controlled tree.
  New production `actors.py`, `process_child.py`, and `engine.py` register native
  actors durably before acknowledging an isolated `-I -S` exec barrier. Every
  engine command attaches to a fresh prestarted, registered AWS provider (pinned
  Pulumi 3.263.0 / AWS provider 7.47.0). No-AWS local backend preview plus logical
  provider up/refresh/destroy passed: 11 commands, all actors stopped. This is
  supplementary feasibility evidence, not a completed live AWS gate.
  Three real-process tests pass: lost successful registration response forbids
  exec; dead creator's registered orphans stop before claim transfer; creator
  death closes the unacknowledged launcher's sole barrier writer.
  Source-review fixes applied: callback errors keep draining pipes; partial
  provider port output is bounded; engine operation/stream waits are bounded
  (600s/5s); recovery refuses unregistered command fallback; inherited AWS and
  Pulumi settings are stripped; exact journal-owned backend context is checked;
  pre-create startup cleanup verifies empty owned AWS effects and disposes key
  and metadata without selecting an uncreated stack. Final source/test review,
  provider fallback failure proof and real G2 create/recovery/cleanup are pending.
  No new AWS resources or privileged host changes in this continuation.

- Registered-native-actor source/test reviews cleared after fixes. Targeted
  regression suite: 349 passed, 86 warnings (6.29s); later tunnel slice excluding
  old process-tree tests: 108 passed, 74 warnings. Local registered-provider
  preview/logical up/refresh/destroy rerun PASS with 11 fresh provider/engine pairs.
  Key creation now records intent before SSM writes and retains uncertain absent
  results. New reviewed `spikes/dev-vpc-v1/aws_access_lifecycle.py` uses production
  AccessUnit against an independently tagged disposable SDK VPC/target fixture.
  UUID owner `08b99fc7-41b1-486c-8c0b-ad7f9a6c71fa`, default profile/us-east-1,
  Home bucket `stlv-state-c054b7aca79c`; local durable WAL/evidence directory
  `spikes/dev-vpc-v1/build/access-lifecycle/<owner>/`. Command `run` performs
  create/SSM readiness/normal stop/full disposal; `recover` with the same owner
  resumes cleanup on failure. Each fixture create records intent before mutation;
  exact ID/graph checks and metadata-only retry protect partial cleanup. No new
  native host installation is needed. Live result is pending; remove all resources
  and namespace versions before moving on.

- First P2 run owner `08b99fc7-41b1-486c-8c0b-ad7f9a6c71fa` stopped
  after fixture creation and before any access/journal/key creation: direct
  `AccessIntent.from_dict(intent.to_dict())` failed because the writer preserved
  tuple targets while the reader requires JSON lists. Fixed `to_dict()` to emit
  list targets and added round-trip regression; state/program/backend 19 PASS.
  Fresh-process `recover` exited 0: application baseline preserved, exact-ID
  and owner-tag absence confirmed for VPC/subnet/IGW/route table/target group.
  No access/native resources or AWS namespace/key were created in that run.
  Retry uses new UUID `49268c39-96e5-4f02-862a-e451e2297068` with the same
  default profile/us-east-1/Home bucket. Remove all resources on completion.

- P2 live owner `49268c39-96e5-4f02-862a-e451e2297068`: production
  registered runner/AccessUnit created instance `i-0eb3c7b398f04a1f4`, group
  `sg-0dc4a2c8147f3f650`, actual roles/profile/document/volume and target ingress.
  Fixed SSM identity/readiness command PASS. Checkpoint provider validation
  accepted the actual credential-free account/region provider state. Normal
  stop removed AWS access resources but failed final inventory: terminated EC2
  instances can lose VPC/subnet fields. Fixed validation to require exact saved
  IID when termination removes context, still reject contradicting retained
  fields or unknown terminated identities. Cleanup/unit regression 25 PASS.
  Fresh-process `recover` (session51940) exited0: original native actor set death
  checked before claim takeover; SDK cleanup, independent refresh/destroy,
  versioned backend removal, generation-bound SSM key removal and all owner
  metadata removal completed. Application target baseline preserved; fixture
  exact-ID and owner-tag absence confirmed. All native actors stopped. This is
  a failed initial run followed by successful recovery, not uninterrupted PASS.
  No DocumentDB, macOS helper, routes or resolver changes in P2 proof.

- 2026-10-05 implementation request authorizes work from scratch on
  `feature/documentdb-vpc-v1`. User initially reserved live proofs for manual
  execution, then explicitly authorized the agent to run the remaining no-AWS
  proof steps. Latest user instruction authorizes the agent to run the AWS
  ownership proof using the default profile and remove all resources afterward.
- Baseline: tracked checkout clean, no `stelvio/dev` or bastion implementation;
  example declares `bastion=True` which the current Vpc constructor cannot accept.
  `.venv/bin/python -m pytest tests/aws/test_vpc.py
  tests/aws/function/test_function_vpc.py tests/bridge -q --disable-warnings`:
  158 passed, 86 warnings. Host macOS 15.7.5 arm64, Go 1.25.3.
- P0 candidate: compiled native broker creates utun and passes its descriptor
  to a separate non-root gVisor-based forwarding process. Reject the prototype's
  hand-written TCP implementation and privileged project Python runtime.
  Candidate dependencies and source live in `spikes/dev-vpc-v1/`, not production.
  tun2socks v2.6.0 generic fd adapter does not handle Darwin's utun family header;
  proof must explicitly strip/prepend the 4-byte network-order family header.
  Packaging/forwarding are candidates, not selected or proven support claims.
- P0 artifacts ready: `spikes/dev-vpc-v1/native/broker/main.c`, `native/main.go`,
  `native/main_test.go`, locked Go dependency files, `build.sh`, and `probe.py`.
  `build/` is ignored and can be reproduced. No production files changed.
- Checks: C strict compiler/build PASS; Go build and `go vet .` PASS;
  `go test -race -count=1 -timeout=45s -v .` PASS (single two-VPC behavioral test,
  4.95s test/6.17s package). Probe Ruff format/lint PASS. Kernel-table read-only
  `broker --check` accepts `10.254.0.0/16` + `10.253.0.0/16` and rejects the
  current LAN `192.168.68.0/24`. These are not live utun/AWS evidence.
- Sequential source/test reviewer agents found and resolved bounded ACK,
  ambiguous route-operation journal retention, READY wait budget, SOCKS wrapper
  hiding CloseWrite, both-direction half-close bounds, and test packet-device
  saturation/cleanup issues. Final test review has no proof-blocking finding.
  The source review's last requested common socket deadlines and late route
  recheck were applied, then built and tested.
- Simulator finding: Darwin Unix datagram sockets return ENOBUFS on bursts;
  a retry wrapper remained unreliable. The test now uses bounded copied packet
  queues, preserving framing/backpressure without claiming real utun behavior.
- Current artifacts: broker SHA256
  `d0664148310e34ad3cd67878c45f1307341fcc8bd8125419cedb17e068eda0d6`;
  forwarder SHA256
  `41fc9adefa71569e05b5d9169d2415940aea1a4a2230197b96ef73b4f5ed97aa`.
  Native broker links only system libSystem. Go forwarder links system
  libSystem/libresolv. No project/venv interpreter is in the binary runtime.
- First live no-AWS proof PASS on 2026-10-05 (README steps 1–5).
  Installed broker at `/Library/PrivilegedHelperTools/dev.stelvio.vpc-proof`,
  root:wheel 0755; installed/build SHA256 match the recorded artifact.
  `sudo -n` required a password, so explicit macOS authorization via system
  `osascript` ran native install and broker commands. The broker command supplied
  `SUDO_UID=502`; Unix peer authentication verified developer UID 502 and the
  forwarder authenticated root. No project interpreter was elevated.
- Live run: developer forwarder PID 99606 UID 502; installed broker PID 99649
  UID 0; `READY uid=502 interface=utun7 vpcs=2`. `probe.py` passed both
  `10.254.10.10:8080` → `vpc-1` and `10.253.20.20:8080` → `vpc-2`, each with
  1,048,576 exact echoed binary bytes and EOF. This proves actual OS sockets
  through utun to distinct local SOCKS stand-ins, not real AWS VPC resources.
- Teardown PASS: SIGINT to the identified developer forwarder; forwarder and
  broker both exited 0; broker reported deleting both CIDR routes. Kernel route
  table has no proof ranges/TEST-NET route; `ifconfig utun7` reports absent.
  Elevated listing confirms no `broker.sock` or `journal`, only inactive root-
  owned `lease` (0600) in the proof directory (0711). Installed native helper
  remains, as prescribed by the runbook. No AWS resources or DNS state changed.
- Second live local lifecycle proof PASS (2026-10-05): new broker `--reconcile`
  and `--uninstall`. Verified old installed hash before inactive upgrade via
  macOS authorization. New broker SHA256
  `017b1724d4e8d82bdc9084e7c973b8c1c685f2b26bccc08808f45095626a9102`;
  rebuilt forwarder `161dd3900602041edbaa4b7c45d74e306050d89b461df49b0653277f004f1955`.
  Forwarder PID3698 UID502 launched from `/private/tmp`; root broker PID3717;
  READY utun7 with both 1MiB probes PASS. Active uninstall refused. Deliberate
  broker SIGKILL caused forwarder EOF exit and removal of utun/routes. Retained
  root journal recorded caller UID, both intents, interface and applying entries.
  Reconcile removed stale owned socket/journal; uninstall removed native helper
  and local proof state, both absence checks PASS. No helper/session remains.
  This proves working-directory independence, not deleted-installer-venv or
  product CLI acceptance. Source review removed unjustified old-loaded-generation
  protection claim: P0 excludes concurrent replacement; production validation
  of installation generations remains open. No AWS/DNS state changed.
- Prepared `aws_ownership.py` and `AWS-OWNERSHIP.md` for serial user-run proof:
  coherent two-VPC customization, separate app/access stacks in owner-specific
  S3 AWSHome prefix, immutable remote intent, backend locks, SSM recovery key,
  account-bound providers, checkpoint-only partial cleanup. Retains key/state
  for recovery. Source review fixed partial cleanup, credential drift, key loss,
  and provider account constraint serialization. Hermetic tests caught callback
  replacement losing computed subnet args; fixed by preserving those args.
  Four tests PASS and Ruff PASS. Sequential test review tightened exact access
  group inputs to reject inline rules/missing tags. **No AWS proof has run**;
  this is preliminary ownership, without bastion/SSH/DNS/hard-crash evidence.
  Combined new proof/VPC/Function/bridge suite: 162 passed, 86 warnings (2.30s).
  Final branch check: `feature/documentdb-vpc-v1`; native helper and state absence
  reconfirmed. Preserve concurrent user Git/index changes; agent made no commit.
- First live AWS ownership attempt used explicit profile `default`, configured
  region `us-east-1`, existing AWSHome bucket `stlv-state-c054b7aca79c`, owner
  `c76ab71b-58a0-4bce-98fc-d4e2d59e1ac9`. App/access creation and access deletion
  passed, but verification exposed Pulumi omitting `resources` in an empty export.
  Fixed with `.get("resources", [])`; focused regression/test review PASS,
  five proof tests PASS. Finally teardown succeeded for access/app. AWS tag scans
  confirmed zero VPCs/subnets/IGWs/groups/route tables/rules; both recorded VPC IDs
  absent. Removed all 509 S3 versions/delete markers in its exact owner prefix and
  dedicated SSM parameter; both absence checks PASS. Logs/results under ignored
  `spikes/dev-vpc-v1/build/aws-ownership/<owner>/`. Remove proof-only SSM key and
  all S3 versions/delete markers only after resource cleanup and absence checks.
  Existing state bucket/main app remain intact. Corrected rerun completed with
  fresh owner `6b31c848-b4b1-4479-b1d2-ba31645fc909`, same default/us-east-1/bucket.
  Rerun verification PASS: access resources deleted, application checkpoint
  unchanged, EC2 VPC CIDRs intact/no target ingress, preview reports 48 unchanged.
  Recorded VPC IDs: `vpc-0abb0e77c2564f56e`, `vpc-0a3bb6971e0f2a002`.
  Final fixture teardown PASS (48 application/6 access Pulumi resources deleted).
  All seven runner phases exited 0. Final AWS tag scans: zero proof VPCs/subnets/
  IGWs/groups/route tables/rules, plus explicit recorded VPC ID absence. Removed
  509 S3 versions/delete markers in the second exact owner prefix and its SSM key;
  zero metadata remains. Both attempts fully cleaned, existing AWSHome bucket
  preserved. Results/cleanup evidence in ignored `build/aws-ownership/<owner>/`
  (`results.json`, `cleanup.json`, seven phase logs). No cross-venv or interrupted
  creation proof claimed; this establishes ordinary independent access ownership
  and deletion with an unchanged application fixture. Branch remains requested
  `feature/documentdb-vpc-v1`; five focused proof tests/Ruff pass after the fix.
- G0 incomplete; P1–P7 not started. AWS transport,
  private DNS, full installed lifecycle, production recovery, and independent
  temporary AWS state remain unproven. The example cannot yet run in dev mode.

- Next P0 work: new `aws_transport.py` fixture reuses fresh ownership programs,
  exports public subnet IDs and creates two owned private zones/records. Access
  owns two AL2023 t4g.nano bastions, encrypted/deleted-on-termination root volumes,
  dedicated nonroot `stlv-tunnel` user (no sudo, no session channels), no SSH
  ingress, SSM role/profile and fixed host-identity document. Client uses EIC
  ephemeral keys, authenticated SSM bootstrap, pinned OpenSSH over SSM, bad-pin
  rejection, reconnect pin comparison, and full proxy process-group cleanup.
  AWS credentials remain nonroot; no handler/runtime production migration.
  Source review fixed ready-marker race and false SOCKS readiness. Test review
  tightened independent document contents, IAM trust/policy and profile linkage.
  Seven proof tests pass; broader baseline check running. No prototype copied.
  New proof owner `9f9a5f6a-f7d8-42cf-b722-178eb0657910`, default/us-east-1,
  bucket `stlv-state-c054b7aca79c`, pinned normal Amazon AL2023 arm64 AMI
  `ami-065b1b834d2a83a7a` resolved read-only from the AWS service SSM parameter.
  Deployment ran: 52 app / 14 access resources created; authenticated SSM
  bootstrap succeeded and incorrect SSH pin was rejected. Client cleanup then
  failed on Darwin `killpg` EPERM for an exited proxy group. Both stacks destroyed,
  ordinary ownership verification passed, all recorded VPC/IAM/zone/document IDs
  absent and instances terminated; zero tagged volumes/network resources remain.
  Removed 591 exact-prefix S3 versions/delete markers and dedicated recovery key.
  No native helper installed or host route/DNS change. Local reproduction and
  corrected cleanup check pass: tolerate EPERM only for no live group members,
  otherwise fail; never inspect argv because plugin arguments contain tokens.
  Runner `/private/tmp/stelvio-transport-run.py` attempts
  teardown on setup failure, client exit, completion marker or 30m bound. If
  deployed, preserve exact owner metadata until resources/SSM sessions are gone;
  remove all exact-prefix S3 versions and dedicated recovery key afterward.
  Logs/inventory/manifest under ignored `build/aws-transport/<owner>/`.

- Fresh transport owner `8c013a88-9733-4919-bc6b-28946c793034` is LIVE in
  default/us-east-1, same bucket/AMI. Runner `/private/tmp/stelvio-transport-run.py`
  exec session 75179 owns teardown; its `done` sentinel must be written ONLY after
  host forwarding stops. Instances `i-0250cf2b761872bda`, `i-044b4aeb1560cdb5b`.
  Native helper installed SHA256 `c9a88743fd53bb6ca7a9edd1153ed02d38732c15a40d73e2b4cc88fc788d107e`;
  forwarder PID21138 UID502 / session56684, broker PID21158 UID0 / session25213,
  utun7; DNS relay session28772. Do not print SSH/plugin argv (session tokens).
  Root broker now supports fixed `--dns-owner UUID` only for these two proof CIDRs:
  owns exactly two UUID-derived files in preexisting `/private/etc/resolver`,
  O_EXCL/no-follow, journals root-owned inode identity before contents, cleanup
  compares inode and expected contents. Unrecorded creation ambiguity retains
  the journal. Source review found fstat-after-create gap; fixed ambiguous flag.
  DNS relay nonroot UDP/TCP loopback10890/10891 → own SOCKS → VPC .2 TCP53,
  suffix-isolated, bounded threads, SERVFAIL on path failure, no public upstream.
  Real SSM bootstrap and both incorrect pins rejected. Actual macOS getaddrinfo
  returned correct private addresses; ordinary hostname TCP probes both echoed
  1MiB + EOF. Direct relay UDP/TCP and unrelated public DNS passed.
  Evidence `network-proof.json`; first outage rejected private OS lookup while
  other VPC/public DNS passed, but immediate restoration hit macOS negative cache.
  Subsequent bounded recovery and complete hostname/network probe passed.
  Repeating outage with filtered native packet capture on primary public DNS
  (`1.1.1.1`/en0), matched only this fixture UUID, to distinguish actual
  no-public-fallback evidence. tcpdump PID24866 UID0, root pcap/log under
  `/private/tmp/stelvio-proof-8c013a88*`; stop/remove after capture verification.
  Host/AWS cleanup pending.
  Broker SIGKILL proof passed: forwarder exited on EOF, utun7/proof routes vanished,
  stale inode-journaled DNS files reconciled, helper uninstalled, resolver directory
  empty. DNS relay stopped; AWS access stack destroyed, app teardown underway.
  Repeated outage retry loop exhausted its bound (actual loop included per-call
  wait, so exceeded nominal60s), later independent raw DNS/getaddrinfo and both
  hostname1MiB probes passed. Automatic restoration/cache handling remains open.
  Filtered pcap contains zero packets but lacked a positive filter control, so
  do not claim complete no-public-fallback evidence from it. tcpdump ignored INT/
  TERM in its detached process; exact PID KILL requested. Local wire tests now
  assert exact A/TXT RRsets, UDP<=512/TC, no foreign-suffix accepted connections,
  empty REFUSED/SERVFAIL answers; 9 proof tests PASS, Ruff PASS.
  FINAL cleanup verified: both stacks deleted, explicit VPC/IAM/zone/document IDs
  absent, instances terminated, no tagged volumes/network resources; 591 exact-
  prefix metadata versions deleted, recovery key absent. No SSH/plugin/capture/
  helper process remains, no helper/state/resolver files remain. Ephemeral SSH
  key directory removed. Evidence retained only in ignored build/<owner> logs.
  Controller exit1 came from its30m bound at20:56, which correctly triggered full
  AWS teardown. Phase results all0; this is not a complete proof-run PASS. The
  late restoration retries overlapped that bound, so separate OS cache delay from
  fixture teardown in the next local reproduction; do not assert a diagnosed
  cache cause from this run alone. Save per-check timestamps/terminal state in
  future proof runners and reserve teardown time before the session deadline.

- No-AWS follow-up LIVE: owner `8966eb15-a3b6-4b24-a6b5-998b490c264b`.
  `local_dns_probe.py` uses bounded local SOCKS/DNS stand-ins, a persistent
  cancellable subprocess for OS DNS/hostname TCP (avoids unbounded getaddrinfo),
  and finite graceful→kill child cleanup. Source review found those two bounds;
  fixed before root operation. Known native helper hash installed from isolated
  installer-A, then installer-A including its source/artifact deleted. Independent
  stdlib-only caller-B `/private/tmp/stelvio-vpc-8966eb15/caller-B` invokes native
  forwarder; normal project Python handles local fixture only, never root.
  Local driver session13786; native broker via macOS authorization session55634.
  Output `build/local-dns/<owner>/`. Must stop children, check routes/resolver files,
  native uninstall, remove caller-B/temp fixture after evidence. No AWS resources.
  First local follow-up invoked helper/forwarder through caller-B after A removal,
  hostname1MiB probe PASS; fixture outage assertion wrongly disallowed a cached
  valid address. Normal cleanup removed routes/utun/resolver files. Corrected to
  query a fresh private hostname, as specified. Rerun output `<owner>-attempt-2`,
  driver session46113, broker session87596, caller forwarderPID32836; live now.
  Corrected local diagnostic PASS: fresh private lookup rejected, other VPC1MiB
  path healthy; same persistent resolver child recovered automatically at
  elapsed84.221s (transport restored6.100s; ~78.1s recovery), subsequent hostname
  1MiB PASS. No AWS/different client intervention. All children stopped at85.708s,
  native broker normal route/DNS cleanup PASS; utun7/resolver files absent,
  caller-B/temp fixture removed. Uninstall requested (session85684). This proves
  eventual bounded DNS recovery on this macOS host and deleted-installer-venv
  runtime independence, not immediate recovery or product CLI acceptance.
  Combined proof/VPC/Function/bridge suite167PASS86warnings6.41s; Ruff PASS.
  Native generation/fencing, controlled no-fallback capture and interrupted AWS
  recovery remain before G0. The first late raw query after fixture shutdown
  timed out; it adds no diagnostic result. No fresh AWS deployment in follow-up.

- Native loaded-generation fence added: `proc_pidinfo(PROC_PIDREGIONPATHINFO)`
  on the helper's own code mapping compares vnode device/inode with installation;
  rechecks after acquiring the installation lock. Strict compile/source review
  PASS. Read-only old-loaded-image proof PASS: atomic inactive replacement from
  inode69986205→69986265 (same device16777230), old process refused, new image
  accepted. Broker current SHA256
  `9932a6f10bf332d3d40a6910a769ef2714c6c481416638f013f6a7d5d6f3a9eb`.
  Evidence `build/generation-proof/results.json`. Native helper is installed
  INACTIVE, awaiting controlled no-fallback follow-up then uninstall. No root
  helper session/host changes or AWS resources. Production installer still must
  honor the artifact lock and use atomic replacement; in-place rewriting is
  outside this proof. New read-only diagnostic `--wait-generation-check` accepts
  only a bounded release byte, never operations/paths.

- Controlled no-fallback local follow-up owner
  `9c8129cf-324c-4e8e-aa78-ac3af3a7c28a`: caller venv
  `/private/tmp/stelvio-vpc-9c8129cf/caller`, installed helper hash9932a6f...
  Root capture PID35486, en0→primary1.1.1.1 UDP/TCP DNS filtered only this UUID's
  service/fresh-service names. Positive controls produced8 packets (queries+
  replies for UDP/TCP each name); initial0 snapshot was before kernel capture
  buffer delivery, not a failed filter. Fixture's120s setup wait ended before
  the start marker, cleaned host state. Rerun output `<owner>-attempt-2` uses
  stable8-packet baseline and the existing validated capture. Must KILL exact
  root capture PID, read final packet count, uninstall native helper, remove
  caller/temp fixture, verify all host state gone. No AWS resources/deployment.
  Rerun PASS: healthy hostname1MiB, uncached fresh private outage rejected,
  other VPC1MiB healthy; persistent worker recovered at88.774s after restoration
  at10.655s (~78.1s), restoredhostname1MiB PASS, children stopped90.263s.
  Positive controls8packets; final capture8packets (0additional public DNS
  queries). Filter validated for both service/fresh-service over UDP and TCP on
  configured primary1.1.1.1/en0. This establishes scoped no-fallback on this host
  for these private names under this outage; no global/platform support claim.
  Evidence results.json/capture-proof.json/capture.pcap. Normal native cleanup
  removed routes/utun/resolver files; caller/temp fixture removed. Exact root
  capture KILL + current helper uninstall requested; verify both completed.
  Final native uninstall PASS, helper/state absent; host remains clean. No AWS
  resources. Controlled no-fallback and generation-proof gates have real evidence.

- Next ownership edge proof prepared in `aws_recovery.py`: SDK candidate for
  exact pending intent before AWS calls, with deliberate hard process exit after
  SG/rule commit before caller IDs are checkpointed. Single small app VPC/target
  plus two temporary cases, account-bound encrypted S3 prefix; recovered using
  planned physical names/VPC/tags/description/port/source against remote intent.
  Run from separate dependency-isolated venv after deleting first venv; preserve
  app identity/ingress, then cleanup all fixture resources and exact state.
  Source review underway. No AWS run for this source yet. This tests the
  unrecorded-API-result boundary, not an interrupted Pulumi engine; production
  backend/reconciliation selection remains explicit before migration.
  Final cleanup script `/private/tmp/stelvio-transport-finalize.py <owner>` checks
  explicit IAM/zone/document/VPC absence and terminated instances, sessions,
  tagged network/volumes before deleting exact-prefix metadata+SSM key.

- All product decisions remain those in the requirements/specification, including
  multiple distinct-CIDR VPCs, partial failure, persistent config forms, and custom DNS.
- Implementation planning must include trusted helper packaging and recoverable
  temporary-resource ownership as early proof gates; neither is assumed proven.
- Live tunnel acceptance needs an opt-in serial macOS lane, separate from the
  parallel VPC tier and the existing Ubuntu-only integration workflow.
- Apply current contributor guides plus `.agents` skills. Behavioral tests and
  Pulumi settlement rules take precedence over copying prototype tests.
- VPC has no CIDR constructor parameter; prove multi-VPC addressing using
  coherent VPC and subnet customization before assuming a new API is needed.
- Preserve supported startup credential sources while isolating handler
  environment mutation; do not copy the prototype's provider exclusions.
- The earlier document-creation request authorized no runtime work. The current
  implementation request supersedes that limit. The user authorized the agent
  to run the no-AWS privileged proof and now this default-profile AWS proof. No branch changes,
  commits or pushes have been requested or performed.

- Lost-success-response AWS recovery proof PASS, default/us-east-1, owner
  `a514816c-228e-477a-b425-63575cca1ff1`. SDK-only creation writes immutable
  account-bound S3 intent before creating tagged groups/rules. Creator exited
  73 after group creation and 74 after rule creation without recording returned
  IDs. Its venv and application creation source were deleted. Separate boto3-only
  recovery venv, with creation function deliberately unavailable, recovered twice
  using remote planned names/tags and preserved application identities/ingress.
  Bounded discovery avoids interpreting eventual-consistency emptiness as absence;
  observed IDs are saved before cleanup; known-ID disappearance is confirmed.
  Explicit final cleanup supports a partially created rule-case group without
  claiming a successful rule proof. Current rule fields are revalidated before
  resumed deletion. Final application teardown PASS, owner-tag scans return zero
  VPCs/groups/rules, all 9 exact-prefix metadata versions removed. Existing home
  bucket preserved; no instances/SSM parameters created. Evidence ignored
  `spikes/dev-vpc-v1/build/recovery/<owner>/results.json` and phase logs.
  Seven behavioral tests pass, including recovery journal-before-deletion ordering.
  This proves SDK reconciliation of unrecorded API results and cross-venv recovery;
  it does not itself prove an interrupted Pulumi engine. Select how that journal
  combines with the independent Pulumi ownership proof before production migration.

- Actual interrupted Pulumi engine recovery PASS on corrected owner
  `722387a6-a01b-4735-8dd9-16c88d16122c`, default/us-east-1. Separate creator and
  recovery environments have only boto3/Pulumi/AWS provider dependencies; no
  Stelvio/application import. After remote group/rule commit marker, recorded
  creator PID42417, engine42464, caffeinate42478 and separately grouped provider
  PID42485 were verified stopped and killed. Discovery persists identities before
  signaling and refuses disappearance/reparenting or unconfirmed stop. Creator
  venv/program deleted; B's creation function unavailable. Dead-owner namespace
  cancel/refresh/destroy PASS, same application VPC/target and no target ingress.
  Final SDK fallback found nothing to remove; application teardown PASS.
  Zero owner groups/rules/VPCs; all 59 backend versions/delete markers plus
  dedicated SSM key removed; final executable-only process check empty.
  Evidence ignored `build/engine-recovery/<owner>/results.json` and
  `interrupted-processes.json`. Earlier owner `62abb3be-470a-4950-a646-312fcf319867`
  recovered/cleaned AWS (59 metadata versions/key) but is NOT accepted for engine
  death: provider40491 survived original-PGID kill and was explicitly stopped.
  This exposed Pulumi's separate plugin groups and prompted the corrected proof.
- G0 selected profile now recorded in linked profile note: native root broker,
  nonroot pinned gVisor forwarding, scoped DNS relay, pinned SSH-over-SSM/EIC,
  separate Pulumi ownership plus immutable pre-create SDK reconciliation.
  Product installer coordination, runtime health/admission and DocumentDB are
  implementation/acceptance work, not falsely claimed by the P0 mechanisms.
  Nine new recovery/process behavioral tests pass; source review final no blockers.

## Not yet specified

- The implementation profile in specification section 10.2 is selected and
  proven by the plan's initial engineering gate, not silently inherited from
  the prototype.

## Out of scope

- Linux/Windows tunnels, overlapping VPC CIDRs, concurrent project tunnel
  sessions, managed/external network modes, and general bridge redesign.

## Next implementation session

1. P7 user docs/changelog/reviews/package checks are complete. For release
   certification, repeat native/AWS acceptance of the exact Go1.27.2 artifacts;
   retain the separate real Intel networking and other-release limitations.
2. Preserve global helper status/install/reconcile/cleanup ownership and EOF
   revocation. Surface typed host refusal/uncertain cleanup and actual retained
   DNS ownership; never purge uncertainty or kill an unfenced creator.
3. Stay on feature/documentdb-vpc-v1, preserve user staging, no unrequested commit
   or push. Native asset rebuild is needed only if its tracked native sources
   change; matching source/hash/package and affected live proofs are required.
