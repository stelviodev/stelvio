# Traforo acceptance tool

Run the Stelvio/Traforo acceptance suite against one frozen commit and an optional
ordered set of patch commits. This is a separate developer package. Stelvio does
not import it, and the Stelvio wheel and sdist do not contain it.

The tool uses the candidate's maintained build scripts and acceptance tests. It
adds orchestration, immediate case reporting, ownership records, recovery, and an
independent cleanup audit. It does not fix the candidate or publish test results
to GitHub.

## Install and choose a run

Use Python 3.12 or later and uv:

```sh
uv sync --project pkg/traforo-acceptance-tool --locked
uv run --project pkg/traforo-acceptance-tool traforo-acceptance stages
uv run --project pkg/traforo-acceptance-tool traforo-acceptance \
  run FULL_COMMIT --patch PATCH_COMMIT --dry-run
```

For a command available outside this checkout:

```sh
uv tool install ./pkg/traforo-acceptance-tool
traforo-acceptance run FULL_COMMIT --repo /path/to/stelvio
```

The repository must contain the requested commits. Fetch them before the run.
The tool resolves full hashes, clones a detached checkout, and applies each
single-parent patch delta in order without commits. Merge commits, empty patches,
and conflicts stop preparation. It does not change your branch, index, or files.
Patch files and a combined diff remain in the run directory.

**With only a commit, the default is full acceptance, including administrator
operations and billable AWS deployments.** Inspect the dry run first. The tool
asks once before live operations. It displays the profile, account, region,
local binary digest, and intended effects. For unattended runs, supply both:

```sh
traforo-acceptance run FULL_COMMIT --yes --expected-account YOUR_12_DIGIT_ACCOUNT
```

Defaults are the `default` profile, `us-east-1`, and Go `1.27.3`. Use `--profile`,
`--region`, and `--go` to change them. No account number is built into the tool.
AWS credentials come from the normal SDK chain for the selected profile. GitHub
authentication, if needed for dependency setup, comes from the existing environment.

## Prerequisites and stage selection

The current adapter supports the `feat/traforo-14` acceptance interfaces. It checks
those interfaces before tests and host changes. A changed interface must have a
reviewed adapter update, not a guessed fallback.

Full acceptance now includes `aws-lambda-vpc`. The candidate must contain PR #411
at `49eafd2a651dabd72b59c60bcea1e33f95b47a42` or its equivalent reviewed interfaces.
Preparation fails before installation or deployment when those inputs are missing.
The runner never fetches or applies those changes automatically.

For local builds, install the macOS SDK, Go, GoReleaser **2.18.3**,
`golangci-lint`, `make`, and Rosetta on Apple Silicon. The existing GoReleaser
builder validates both Darwin images, libraries, size, version, and provenance.
`GOTOOLCHAIN` explicitly selects the requested Go version. The tool does not use
Homebrew or change your default toolchain.

Live acceptance requires macOS 15+, an ordinary user, serial execution, an idle
and certain helper or a clean machine with no helper, and exclusive host access.
The maintained fixture acquires the shared host lock. Approve normal macOS
administrator dialogs when they appear. The tool does not change their timeout,
write sudoers entries, or collect administrator passwords.

The installer uses only the locally built Traforo image through
`STLV_TRAFORO_DEV_BINARY`. The private AWS Session Manager plugin is verified or
installed through the candidate's plugin installer; its current pin is
**1.2.835.0**. The PATH plugin is never selected. AWS acceptance also requires the
usual Stelvio bootstrap and deploy/destroy permissions, including read permissions
for the independent auditor. The tool adds `pymongo` only to the temporary test
environment when AWS tests need it.

```sh
# All checks that do not install a helper or deploy AWS resources:
traforo-acceptance run FULL_COMMIT --only local --only build --only interop

# Build both images, then build/test distributions and actual process interop:
traforo-acceptance run FULL_COMMIT --only interop

# Native host suite, with required setup and build stages:
traforo-acceptance run FULL_COMMIT --only native

# Only the standalone AWS scenario, with required setup and artifacts:
traforo-acceptance run FULL_COMMIT --only aws-standalone

# Full run without AWS:
traforo-acceptance run FULL_COMMIT --skip aws

# Change a deadline, in seconds:
traforo-acceptance run FULL_COMMIT --only native --timeout native=5400
```

`--only` and `--skip` can be repeated. Stage groups are `preparation`, `local`,
`build`, `distribution`, `native`, and `aws`. Dependencies are added automatically;
excluding a required dependency is an error. Selecting AWS does not select the
native suite or all unit tests. A partial pass is not full acceptance.

| Stage | Work | Default deadline |
|---|---|---|
| prepare | Locked Python environment; candidate and tool checks | 15 min |
| python, style | Python regressions; non-writing lint/format checks | 60 min each |
| go-unit, go-race, go-vet, go-lint | Readonly dependencies; structured test output | 60 min each |
| go-fuzz | Five bounded parser fuzz targets | 15 min |
| build | Both local Darwin images and build provenance | 30 min |
| distribution | Wheel and sdist | 60 min |
| interop | Archive, isolated wheel, DNS/runtime and local SSH interoperability | 60 min |
| native | Installer, routing/DNS, lifecycle and administration suite | 60 min |
| aws-lambda-vpc | Seven shared-app, managed/disabled/non-VPC Lambda cases | 2 hours |
| aws-standalone | Database access, reconnect, SIGINT and owner death | 4 hours |
| aws-dev | Function URL to local handler to DocumentDB | 4 hours |
| aws-host-key | Key change rejection and new-session recovery | 4 hours |
| aws-non-vpc | Real dev invocation without an installed helper | 2 hours |

These are deadlines, not expected runtimes. The CLI prints estimates, completed
stage counts, elapsed time, latest case/phase, last output age, and ten-second
heartbeats. Recovery and SDK audit work also have heartbeats. Teardown can take
an hour per application. Commands receive SIGINT at the deadline, then bounded
TERM/KILL escalation against a verified process identity.

Release-download acceptance and the downloaded-release upgrade case are excluded.
No Traforo release is downloaded. No test compiles Go from pytest; compilation
is a separate runner stage.

Keep the default private user-temporary directory. If you set `--run-dir`, it
must be a new private directory with secure ancestors. OpenSSH StrictModes
rejects paths under writable ancestors such as `/private/tmp`; the tool checks
this before the SSH interoperability stage. Evidence is retained, not deleted.

## Multi-VPC Lambda networking stage

`aws-lambda-vpc` runs all seven tests in one pytest process. They share one
application with two managed VPCs, a third VPC with `bastion=False`, and a fourth
client without VPC attachment. The application has four client Functions, two
backend Functions, two deployment-owned bastions, two private REST APIs, and two
interface endpoints. The plain clients make no private API request. No DocumentDB
cluster or `pymongo` installation is needed for this stage. The independent
Lambda inventory does not require DocumentDB read permissions.

```sh
traforo-acceptance run COMMIT --only aws-lambda-vpc

# Networking acceptance, including the separate non-VPC case:
traforo-acceptance run COMMIT --only aws-lambda-vpc --only aws-non-vpc

# Apply all three incremental commits to the original stack candidate:
traforo-acceptance run f87bd5bcb6c155c875b1ac8685da1fda74b821c9 \
  --patch ea1d914483baf87ae43ec569e041d082bb8f799d \
  --patch e196a31d46bc90e9857f3c72b89ed58c9a64cc3a \
  --patch 49eafd2a651dabd72b59c60bcea1e33f95b47a42 \
  --only aws-lambda-vpc

# Explicit omission makes the report a partial round:
traforo-acceptance run COMMIT --skip aws-lambda-vpc
```

Alternatively, select `49eafd2a` itself as the candidate. Its delta alone is
insufficient on the original stack: it builds on `ea1d9144` and `e196a31d`.

The suite checks simultaneous private HTTPS access, distinct DNS views and
interfaces, SIGINT/SIGKILL, transport recovery, and a stopped bastion with a
healthy peer. Dev invocations must be rejected before handler entry for the
failed VPC. The host-key case checks persistent rejection across runtime restarts
without accepting a changed key. The disabled and non-VPC clients must return
fresh nonces from local handlers during network failures and runtime restarts.
The disabled VPC emits exactly one startup warning and has no managed network unit.

A separate dev lifecycle case kills the actual `stlv dev` command with SIGKILL,
checks cleanup, starts it again, and invokes all four clients with unchanged pins.
Another case attempts standalone startup twice with a changed host key. Each
attempt must exit with status 1, name the instance and trust-reset command, never
report `Network READY`, and leave no owned transports or host networking state.
After the fixture restores the original key, a fresh command must connect.
Normal tunnel exit leaves both bastions intact.

The separate `aws-non-vpc` stage proves clean-machine/no-helper operation. Plain
clients in this shared managed dev session do not replace that check.

The stage runs before the other AWS stages in a full round. It supplements the
DocumentDB stages, which still check production resource discovery/readiness,
exact endpoint/member DNS ownership, database TLS, replica discovery,
authentication, and read/write behavior. The two-hour deadline includes fixture
teardown; the 10–40-minute estimate is provisional, not a live validation result for this tool.

Permissions include the normal tunnel deployment permissions, private API
Gateway administration, interface endpoints, Lambda/IAM/log administration,
EC2 stop/start for proof bastions, and SSM Run Command for the host-key fault.
Costs include two bastions, two interface endpoints, six Functions, and the
third VPC’s associated resources until application teardown. There is no third
bastion or API.
The same account confirmation, private plugin checks, local Traforo override,
and exclusive host requirements apply.

The observer records each API before creation and the application before deploy.
API names are `<app>-api1` and `<app>-api2`; their environment tag is `proof`,
which is separate from the application's environment. Each API has its own ID
and receipt. A lost creation reply is discovered through a validated intent,
exact namespace, and ownership tags. An empty listing alone cannot certify an
unconfirmed creation. No creation is repeated automatically.

Recovery attempts both APIs and the application independently. An API failure
must not skip its peer or application destruction. Only explicit API Gateway
throttle errors receive bounded retries: at most ten attempts within 90 seconds.
Uncertain deletions receive a state read, not a repeated mutation.

The independent audit checks both APIs, endpoint IDs, captured interface IDs,
volumes, all six Functions, all three VPCs, logs, IAM, sessions, and host
restoration. Each captured Function name and VPC ID is checked separately; a
missing managed resource cannot hide a remaining plain client or disabled VPC.
Reports include all seven cases and phases with timings, separate VPC views, and
fault entry/exit records. No private key material is recorded. If a fault is interrupted,
recovery removes the owned application without restarting bastions or resetting
SSH trust. It retains unresolved ownership and cleanup errors.

## Evidence and failures

Each run has a schema-versioned `run.json`, frozen source/patch identities,
archived observer source and fingerprint, artifact hashes, `events.jsonl`,
`run.log`, per-command `command.json` and timestamped `output.log`, JUnit XML,
fuzz inputs, and deterministic `report.json` / `report.md` files.

The external pytest observer records setup, call, and teardown immediately. A
teardown failure is visible before the rest of the session finishes. Failure
records include node ID, phase, file/line when available, and the full traceback.
Reports retain passed, failed, and skipped test phases as well as their timings.
Go records retain package/test failure identity. Commands retain exact argument
arrays, working directory, selected nonsecret environment values, exit status,
runtime, and the output needed to reproduce the failure.

Only passive diagnostics run automatically. No DNS flush, active DNS probe,
implementation change, or automatic test retry can turn a failure into a pass.
Reports separate observed causes from unresolved boundaries: an assertion and
its failing frame are evidence, not proof of an arbitrary underlying root cause.

Logs are private and redact known environment secrets, common password/token
fields, and MongoDB URL passwords. Review logs before sharing: arbitrary test
output cannot be guaranteed safe by a generic redactor. Credential values are
not part of the recorded environment or reproduction commands.

The report has five fixed sections: identity/tools/artifacts; stage/command
results; failures/reproduction; recovery; independent audit/limits. A later
successful recovery never erases a failed test. Audit attempts remain recorded,
including an API timeout followed by a successful read. Opt-in skips in the
ordinary Python suite are expected; skips in explicitly selected acceptance
stages prevent acceptance.

## Resume, cleanup, and audit

```sh
traforo-acceptance resume RUN_DIRECTORY
traforo-acceptance cleanup RUN_DIRECTORY
traforo-acceptance audit RUN_DIRECTORY
traforo-acceptance report RUN_DIRECTORY
```

Resume continues pending stages only after completed stage boundaries. It checks
source, observer, and artifact fingerprints. Failed or interrupted tests require
a new run. They are not silently retried. Artifacts within the same frozen run
are reused after verification. Run records are locked against concurrent owners.

Ctrl+C first asks the current command to stop. Recovery then handles registered
proofs and restores the initial helper image or its initial absence. A killed
runner can leave work for `cleanup`; retain the directory and all ownership
records. `cleanup` prompts before live recovery; use `--yes` only when intended.
It verifies the recorded AWS account and exact project marker before destroy.
It never force-unlocks deployment state or deletes ownership journals to pass.

Applications are recorded **before deploy**, including partial deployments. Root
volume and managed-secret identities are captured before destroy. Recovery uses
normal `stlv destroy` and the existing helper replacement transaction. Only
captured proof Lambda log groups can be removed, after their functions are absent.
Foreign resources, unknown images, changed process identities, live sessions,
and uncertain ownership are retained with an error. Authorization failure does
not remove the original image backup; rerun `cleanup` when you can approve it.

The independent auditor uses its own OS inventories and bounded AWS queries,
not the candidate's cleanup assertions. Host checks include the helper's
read-only status, image, interfaces, routes, raw/normalized scoped DNS, resolver
file identities, service, processes, and administration remnants. AWS checks
cover proof-tagged/name-owned network, database, Lambda, IAM and SSM resources;
captured untagged volumes and managed secrets; log groups; and active sessions.
Shared AppSync APIs must remain. A failed API call means **unknown**, not absent.
Missing pre-destroy inventory also prevents certification.

SSM functional inactivity and delayed History finalization are separate findings.
A `Terminating` History record is not falsely reported as `Terminated`. Cleanup
cannot be disabled once live effects are possible. A partial run is reported as
`partial-passed`; only all selected checks plus certified cleanup can pass.

## Develop and validate this package

```sh
uv sync --project pkg/traforo-acceptance-tool --locked
uv run --project pkg/traforo-acceptance-tool ruff check pkg/traforo-acceptance-tool
uv run --project pkg/traforo-acceptance-tool ruff format --check pkg/traforo-acceptance-tool
uv run --project pkg/traforo-acceptance-tool pytest pkg/traforo-acceptance-tool/tests
uv build --project pkg/traforo-acceptance-tool
```

Unit tests use fake AWS clients and host operations. They cover boundaries,
ownership, deadlines, reporting, and recovery without changing host networking.
Native and AWS orchestration must be accepted in a separate explicitly enabled
run. This package's local tests do not establish native Intel, macOS 26, or AWS
acceptance. See `VALIDATION.md` for the current implementation checks.


## Debian 13

The Linux adapter supports Debian 13 with systemd and systemd-resolved. Use the
Lima setup in `scripts/debian/README.md`. Linux run directories default to a private
directory below `~/.local/state/stelvio/traforo-acceptance`, so OpenSSH StrictModes
can validate every ancestor. Provisioning removes group-write access from the
known guest development and tool-state directories.

The build stage validates ARM64 and AMD64 ELF images. ARM64 runs natively in the
reference VM; native AMD64 validation remains separate. The native stage builds
its failed-installation fixture before pytest, then runs the explicitly selected
Linux suite serially. It checks DNS, packet forwarding, lifecycle, diagnostics,
the terminal monitor, foreign-resource refusal, and installation rollback. The
independent auditor uses Linux kernel, resolved, and systemd inventories.

```sh
traforo-acceptance run COMMIT --only native --yes
traforo-acceptance run COMMIT --only interop
```

These commands do not select AWS stages. Local native acceptance does not need an
AWS account. Unattended AWS stages still require `--expected-account`, verified
against the selected profile. No Linux Traforo release pin is invented: runs use
the validated local image through `STLV_TRAFORO_DEV_BINARY`.

For an interrupted-VM proof, run `scripts/debian/reboot-proof.py prepare DIRECTORY`
inside the dedicated guest, power-cycle the VM after its READY message, and run
`verify DIRECTORY` after boot. Preparation deliberately stops the helper with a
durable test journal. This proof must never run on an ordinary developer host.
