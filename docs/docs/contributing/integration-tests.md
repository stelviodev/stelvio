# Writing integration tests

Integration tests deploy real AWS resources, assert against them with boto3, then destroy
them. They need an AWS profile and take minutes. Unit tests prove we asked AWS for what we
intended; these prove AWS accepted it and the thing works — see
[Writing unit tests](unit-tests.md).

They're the release gate: every component has them, add them for what you add. CI runs them on
manual dispatch only (`.github/workflows/integration-tests.yml`, Python 3.12–3.14), so nothing
runs them on your PR. Run them yourself if you have an account; say so in the PR if you can't.

Read `tests/integration/test_queue.py` and `test_scenario_events.py` next to this page.

## The shape

```python
pytestmark = pytest.mark.integration


def test_queue_subscribe(stelvio_env, project_dir):
    def infra():
        queue = Queue("tasks")
        sub = queue.subscribe("processor", "handlers/echo.main", batch_size=5)
        export_queue(queue)
        export_function(sub.resources.function)

    outputs = stelvio_env.deploy(infra)

    assert_event_source_mapping(
        outputs["function_tasks-processor_arn"],
        event_source_arn=outputs["queue_tasks_arn"],
        batch_size=5,
    )
```

`stelvio_env` deploys `infra()` into its own stack and destroys it on teardown. Nothing is
exported automatically: call the `export_*` helper from `export_helpers.py` for every resource
you assert on, that dict is your only handle on what AWS created. Keys are
`{component}_{name}_{field}`; check `export_helpers.py` for the exact key. `project_dir` is a
temp project with `handlers/` copied in — needed whenever the test deploys a Function.

## Asserting

Assert helpers read the resource back with boto3 and take keyword-only params they check only
when you pass them — extend an existing helper instead of writing a one-off. Most live in
`assert_helpers.py`; that file is too big and being split, so helpers for a new component go
in their own module like `assert_vpc.py`. Get clients from `_boto3_session()`, never build a
session inline.

A green deploy proves nothing about behavior. Invoke what you can: `invoke_lambda` for a
Function, `http_request` for an API — that's how bundling bugs surface.

Never substring-assert. Parse the structure and compare exact values:

```python
# BAD — passes on the wrong field
assert "process-123" in json.dumps(event)

# GOOD
sqs_body = json.loads(event["Records"][0]["body"])
assert sqs_body["task"] == "process-123"
```

## Scenario tests

Property tests read configuration back. Scenario tests prove the wiring fires: deploy the
source plus a subscriber and a results table, trigger, poll, assert the parsed event.

```python
def test_scenario_queue_triggers_lambda(stelvio_env, project_dir):
    def infra():
        results = DynamoTable("results", fields={"pk": "S"}, partition_key="pk")
        queue = Queue("jobs")
        queue.subscribe("worker", "handlers/event_recorder.main", links=[results])
        export_dynamo_table(results)
        export_queue(queue)

    outputs = stelvio_env.deploy(infra)

    send_sqs_message(outputs["queue_jobs_url"], {"task": "process-123"})

    items = poll_dynamo_items(outputs["dynamotable_results_name"])
    assert len(items) >= 1
    event = json.loads(items[0]["event"])
    sqs_body = json.loads(event["Records"][0]["body"])
    assert sqs_body["task"] == "process-123"
```

Triggers: `send_sqs_message`, `publish_sns_message`, `upload_s3_object`, `put_dynamo_item`,
`invoke_lambda`, `http_request`. Waits: `poll_dynamo_items`, `poll_sqs_messages`, `drain_sqs`,
`wait_for_event_source_mapping`. Handlers are pre-built in `handlers/` (`echo`,
`event_recorder`, `api_crud`, `invoker`, `queue_sender`, `auth`) — they reference link names
like `Resources.results`, so the component name in your test has to match.

## Tiers

| Tier | Marker | Flag | Needs |
|---|---|---|---|
| Standard | `integration` | `--integration` | AWS profile |
| VPC | `integration_vpc` | `--integration-vpc` | AWS profile (4 workers max; account VPC quota is 5 including the default VPC) |
| macOS privileged | `integration_macos_privileged` | `--integration-macos-privileged` | macOS 15+, ordinary user, administrator authorization, exclusive host, `-n 0`; never in `run_all.sh` |
| Tunnel AWS | `integration_tunnel` plus `integration_macos_privileged` | Both `--integration-tunnel` and `--integration-macos-privileged` | Same host prerequisites plus AWS profile; billable disposable applications |
| CloudFront | `integration_cf` | `--integration-cf` | AWS profile |
| DNS | `integration_dns` | `--integration-dns` | + `STLV_TEST_DNS_DOMAIN`, `STLV_TEST_DNS_ZONE_ID` (optional `STLV_TEST_ACM_CERTIFICATE_ARN` for a pre-issued `*.domain` cert). One `*.domain` cert plus its validation record stay in the account for reuse (`stelvio:env=test` only, no `stelvio:app`, so cleanup skips them). |

CloudFront distributions take 3–5 minutes to delete, so they get their own tier; their property
tests skip edge propagation with `customize=NO_WAIT_DEPLOY`. DNS tests skip themselves when the
env vars are missing. `run_all.sh` is the single source of truth for test/worker counts —
they're picked so tests divide evenly with no straggler; update them there when you add tests.

The VPC tier holds infrastructure-only tests that construct a `Vpc` (VPC tests, VPC Function tests, and
DocumentDB). Actual CLI tunnel scenarios run in the separate exclusive tunnel tier. The `integration_vpc` marker takes precedence over the standard `integration`
marker, so `--integration` skips VPC tests and `--integration-vpc` selects them. Do not put
VPC creates back in the standard tier: the account allows 5 VPCs per region and already has
a default VPC.

## Running them

```bash
# ordinary tiers in parallel (DNS only if domain vars are set)
STLV_TEST_AWS_PROFILE=<profile> ./tests/integration/run_all.sh

# one tier — take -n from the matching line in run_all.sh
STLV_TEST_AWS_PROFILE=<profile> uv run pytest tests/integration/ --integration -v -n <N>
STLV_TEST_AWS_PROFILE=<profile> uv run pytest tests/integration/ --integration-vpc -v -n 4

# filter
STLV_TEST_AWS_PROFILE=<profile> uv run pytest tests/integration/ --integration -k dynamo
```

Except for the two explicit macOS/tunnel flags below, never combine tier flags: it opens too many files and the run collapses.
Don't interrupt after `[100%]` — teardown is still destroying stacks. Wait for the summary
line.

A killed run leaves stacks and resources behind:

```bash
STLV_TEST_AWS_PROFILE=<profile> uv run python tests/integration/cleanup.py --tags --names
```

Default is state files in the temp dir; `--tags` scans by `stelvio:env=test`, `--names` by the
`stlv-<hex>-test-` prefix, `--dry-run` shows without deleting, `--region` repeats for
cross-region runs.

## Explicit privileged macOS and tunnel lanes

Every test that changes host routes, interfaces, resolver files or helper installation
requires `--integration-macos-privileged`. Run pytest as an ordinary user on macOS
15+ with no live dev/tunnel session. The fixture refuses uncertain ownership and
serializes across users. It preserves and restores the original helper installation.
Administrator authorization is requested through the ordinary installer.

```bash
# Actual host lifecycle, DNS, packet forwarding and administration; no AWS deployment.
uv run pytest tests/integration --integration-macos-privileged -n 0 -v --tb=short

# Adds actual CLI -> Function URL -> local DocumentDB, standalone and outage tests.
STLV_TEST_AWS_PROFILE=default STLV_TEST_AWS_REGION=us-east-1 \
  uv run pytest tests/integration --integration-macos-privileged \
  --integration-tunnel -n 0 -v --tb=short
```

These are the only integration flags that may be combined. Other tiers and xdist
workers are rejected. `run_all.sh` never executes privileged tests; the former
`STLV_TEST_TUNNEL=1` opt-in has been removed.

Set `STLV_TEST_TUNNEL_PYTHON` to a candidate wheel environment's Python containing
PyMongo. Otherwise the current Python and its sibling `stlv` are used. Set
`STLV_TEST_STELVIO_WHEEL` to the absolute candidate wheel for cross-venv acceptance.
Legacy migration requires an approved `STLV_TEST_LEGACY_HELPER` and its independent
`STLV_TEST_LEGACY_SHA256`. Missing artifacts produce named skips, not acceptance.
`STLV_TEST_TUNNEL_AZS` can select comma-separated zones in the test region.

The canonical app is in `tests/integration/fixtures/tunnel_app`. Applications,
ownership receipts, logs and baseline inventories go into pytest temporary directories,
not `spikes/`. Retain failure evidence; use a unique `--basetemp` for each run.
Never reuse an evidence directory with unresolved cleanup, since pytest clears it.

The native lane covers owner death including frozen children, real helper restart,
100 two-VPC cycles, foreign resolver ownership, safe installation/replacement,
cross-venv operation and cleanup/uninstall separation. AWS scenarios additionally
cover TLS/member discovery/read-write, independent VPC failure, host-key changes,
reconnection, standalone access and non-VPC dev without a helper. Normal session
exit retains deployment-owned bastions; application teardown removes them.

Test existence and dated acceptance are separate. The supported floor is macOS 15;
native Intel and untested OS releases remain pending. See the coverage map and
recorded results in `stelvio/tunnel/spike-coverage.md` and
`stelvio/tunnel/testing.md` in the repository.

Go parser, journal, packet, identity, and native syscall tests belong to the
[Traforo repository](https://github.com/stelviodev/traforo). The source under
`pkg/traforo-client-go` is authoritative for the Go client API and unit tests and is
mirrored to that repository. The external repository owns release builds and Go
implementation CI. Run local Go tests directly from the module. Go source is excluded
from Python distributions.
Pytest covers Python behavior, release download validation and genuine process
interoperability; it never compiles Go or runs native algorithm harnesses.

### Native release validation

Stelvio pins Traforo v0.0.7 download URLs, architecture mappings and SHA-256 values
in `stelvio/tunnel/assets.py`. Installation verifies and caches the host executable
under Stelvio's private `bin` directory, alongside Pulumi and the AWS plugin.
No Traforo executable, developer build script, or Go source is shipped in wheel/sdist.

For development while the release repository is private, explicitly supply a token:

```bash
export GITHUB_TOKEN="$(gh auth token)"
stlv tunnel install
```

This is a testing-shell setup step only. Production reads `GITHUB_TOKEN` from the
environment and never invokes `gh`. Without a token, it requests the public release
URL. Private releases require a token with repository contents read access.
Authenticated downloads use the GitHub release-asset API, with credentials removed
before following cross-origin redirects. All paths enforce the pinned binary digest.

Follow [the end-to-end runbook](https://github.com/stelviodev/stelvio/blob/feature/vpc-traforo-v2/stelvio/tunnel/testing.md)
for clean-wheel, cache corruption/concurrency, native upgrade/crash/cleanup, and AWS
acceptance. Set `STLV_TEST_TRAFORO_IMAGE` to the verified cached executable path
after pre-downloading the release to include the optional real Python/Traforo process tests in `tests/tunnel/test_runtime.py`.
These tests execute the verified cached image; ordinary unit runs do not download it.
Native Intel networking must be tested separately from Rosetta execution.

### Testing local Go changes without a release

Run local Go unit/race tests from `pkg/traforo-client-go`, then from the repository root:

```bash
uv run python pkg/traforo-client-go/scripts/build.py
# Copy the exact export command printed by the script into this shell.
export STLV_TRAFORO_DEV_BINARY='/absolute/path/printed/by/the/script'
uv run stlv dev
```

Install GoReleaser v2.18.3, matching the release workflow. The script uses the shared
GoReleaser configuration to build both macOS architectures with the available SDK,
validates them, and installs the host image through the ordinary
administrator-authorized replacement transaction. `make build` in the Go directory
(or the script's `--build-only` option) validates without installation.
Apple Silicon requires Rosetta for amd64 checks; Intel records arm64 execution as
pending after structural and embedded-version checks.
Stop live sessions before changing builds. It never updates release pins or caches.
The override is process environment input, captured before local handler execution.
Both helper startup and forwarder validation select its digest; the forwarder still
executes the installed root-owned image without root privileges.

Rebuild after edits and set the variable in every shell running managed networking.
Without the variable, managed startup uses the DNS-capable v0.0.7 release.
Restore that pinned release with `unset STLV_TRAFORO_DEV_BINARY` followed by
`uv run stlv tunnel install`.
No persistent override is installed. Older Stelvio versions ignore the variable.
Invalid overrides fail without fallback; inspect and ordinary uninstall still work if
the build file is gone. Source, scripts and output remain excluded from distributions.

For actual Python/local-image interoperability, set `STLV_TEST_TRAFORO_IMAGE` to the
same executable and run `tests/tunnel/test_runtime.py` with the override enabled.
Go compilation is a separate developer step, never performed by pytest.

## Cost and isolation

Each test deploys into its own stack (`integ-<test-name>`) under an app named `stlv-<6 hex>`,
so parallel runs and reruns never collide. Teardown destroys it, retries once after a refresh,
and prints the state dir if it still fails. The resources are real: Lambda, DynamoDB, SQS and
SNS stay near free tier, CloudFront and NAT gateways don't.

## Gotchas

- DynamoDB Streams: after `wait_for_event_source_mapping()` the mapping still has to discover
  shards. Write items in a loop until one lands; write-once-then-poll times out.
- S3 notifications: AWS sends `s3:TestEvent` when the config is created. Sleep, `drain_sqs()`,
  then trigger.
- A bucket that receives objects needs `customize=FORCE_DESTROY_BUCKET`, otherwise destroy
  fails on a non-empty bucket.
- Sleeps are named module constants with a comment explaining the number.

### Traforo DNS migration acceptance

Private DNS serving and wire/probe behavior now belong to the nonroot Go forwarder.
Run Go unit/race tests and DNS/control fuzzing in `pkg/traforo-client-go`. Pytest
covers orchestration and fixed-wire Python/Traforo interoperability without
compiling Go. Native `test_tunnel_dns.py` proves macOS resolver delivery, retained
failure ownership, two-VPC routing/carrier payloads, and stable ports on reconnect.

For release acceptance, leave `STLV_TRAFORO_DEV_BINARY` unset and use the pinned
v0.0.7 image. For Go changes, optionally build locally and export its exact path
in every networking shell. New binaries report one semantic version. Stelvio
requires the pinned base version, allowing `v0.0.7-dev+g<commit>` snapshots only
with the override. Older installed helpers can be retired safely; their old output does not satisfy
the current activation gate. Version validation happens before installation or host changes. The external repository owns release
builds/implementation CI. Follow
`stelvio/tunnel/testing.md` for the ordered native and AWS acceptance runbook.
