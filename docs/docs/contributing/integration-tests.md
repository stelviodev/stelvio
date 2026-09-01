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
| CloudFront | `integration_cf` | `--integration-cf` | AWS profile |
| DNS | `integration_dns` | `--integration-dns` | + `STLV_TEST_DNS_DOMAIN`, `STLV_TEST_DNS_ZONE_ID` (optional `STLV_TEST_ACM_CERTIFICATE_ARN` for a pre-issued `*.domain` cert). One `*.domain` cert plus its validation record stay in the account for reuse (`stelvio:env=test` only, no `stelvio:app`, so cleanup skips them). |

CloudFront distributions take 3–5 minutes to delete, so they get their own tier; their property
tests skip edge propagation with `customize=NO_WAIT_DEPLOY`. DNS tests skip themselves when the
env vars are missing. `run_all.sh` is the single source of truth for test/worker counts —
they're picked so tests divide evenly with no straggler; update them there when you add tests.

## Running them

```bash
# all tiers in parallel (DNS tier only if the domain vars are set)
STLV_TEST_AWS_PROFILE=<profile> ./tests/integration/run_all.sh

# one tier — take -n from the matching line in run_all.sh
STLV_TEST_AWS_PROFILE=<profile> uv run pytest tests/integration/ --integration -v -n <N>

# filter
STLV_TEST_AWS_PROFILE=<profile> uv run pytest tests/integration/ --integration -k dynamo
```

Never combine tier flags in one pytest run: it opens too many files and the run collapses.
Don't interrupt after `[100%]` — teardown is still destroying stacks. Wait for the summary
line.

A killed run leaves stacks and resources behind:

```bash
STLV_TEST_AWS_PROFILE=<profile> uv run python tests/integration/cleanup.py --tags --names
```

Default is state files in the temp dir; `--tags` scans by `stelvio:env=test`, `--names` by the
`stlv-<hex>-test-` prefix, `--dry-run` shows without deleting, `--region` repeats for
cross-region runs.

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
