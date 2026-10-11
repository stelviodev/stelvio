# Local execution after deployment

Use `@app.local_execute` in `stlv_app.py` for local tasks that need deployed resource
names, ARNs, or other [link properties](linking.md). The callback receives resolved
strings with attribute access and generated IDE types.

```python
import sys
from typing import TYPE_CHECKING

from stelvio.app import StelvioApp
from stelvio.aws.s3 import Bucket
from stelvio.config import AwsConfig, StelvioAppConfig

if TYPE_CHECKING:
    from stlv_local import LocalComponents

app = StelvioApp("my-app")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(aws=AwsConfig(region="us-east-1"))


@app.run
def run() -> None:
    Bucket("my-bucket")


@app.local_execute
def prepare_bucket(components: "LocalComponents") -> None:
    print(f"Bucket ready: {components.my_bucket.bucket_name}", file=sys.stderr)
```

For this bucket, `components.my_bucket.bucket_name` and
`components.my_bucket.bucket_arn` are strings. The view includes every registered
component with nonempty link properties and respects custom link creators. It
contains neither live component objects nor the `.resources` tree. Both the root
view and each component's properties are immutable; dictionary access is unsupported.

## When callbacks run

Callbacks run after successful `stlv deploy`, including deployments with no
infrastructure changes. Provisioning, state saving, completion recording, lock release,
and required cleanup have finished. The complete deployment summary, warnings, and
resource outputs have been printed, and deployment spinners have stopped. Failed
or cancelled deployments skip callbacks.
`stlv diff` generates types but does not execute callbacks. `stlv dev`, destroy, and
refresh do not execute them.

Local callbacks are scheduled through the existing `after_deploy` signal. All ordinary
`@app.on(signals.after_deploy)` handlers finish before completion output and local
execution, regardless of registration order. Local callbacks then run in their own
registration order as the final command stage. Sync and async callbacks
are supported, return values are ignored, and repeated registration of the same
callback keeps the first error policy. Register callbacks at module level or in
modules discovered during app execution.

Callbacks use the local process's credentials. Link IAM permissions and linked files
are not applied to local execution. Tasks such as seeding data, running migrations,
or calling APIs need the appropriate local credentials and dependencies.

## Blocking interaction

```python
@app.local_execute
def ask_about_bucket(components: "LocalComponents") -> None:
    answer = input(f"Use bucket {components.my_bucket.bucket_name}? [y/N]: ")
    print(f"Answer: {answer}", file=sys.stderr)
```

Blocking calls such as `input()` are supported. Stelvio waits for the callback before
exiting, and the deployment display no longer redraws while you type. Ctrl+C interrupts
local execution, skips subsequent callbacks, and emits `on_cancel` with
`phase="local_execute"`. The completed deployment is preserved.

## IDE completion

Stelvio generates `stlv_local.py` at the project root when infrastructure is evaluated
by `stlv diff` or `stlv deploy` and a local callback is registered. Import
`LocalComponents` under `TYPE_CHECKING` and quote the annotation, as above, so the app
also loads before the first generated file exists.

The file contains dataclasses and string annotations only, never deployed values.
It describes the latest evaluated environment; a subsequent invocation replaces
the schema and removes stale fields. An app without link properties receives an
empty view. Do not edit the generated file.

Component attributes follow the same naming rules as linking: `my-bucket`,
`my_bucket`, and `myBucket` each become `my_bucket`. Distinct names that normalize to
the same attribute are rejected. Invalid Python property names and reserved generated
names are also rejected, in both local and Lambda link accessors.

## Errors and output

By default, a callback exception fails the command and prevents remaining local
callbacks from running. The infrastructure remains deployed. Use `errors="continue"`
for a callback whose failure should be reported without failing the command:

```python
@app.local_execute(errors="continue")
async def report_bucket(components: "LocalComponents") -> None:
    print(components.my_bucket.bucket_arn, file=sys.stderr)
```

These policies apply to callback exceptions. Failures while collecting properties,
generating types, or resolving values fail the operation. Deployment values must
resolve to concrete strings, even though preview values may be unknown. Secret link
values are available to callbacks in memory; Stelvio does not write them to the
generated file or add them to deployment summaries.

Framework diagnostics go to stderr. Callbacks control their own output; use stderr
or an external destination when using `--json` or `--stream`. Stelvio waits for each
callback and does not impose a timeout, retry it, or roll back its side effects.

The summary appears before local execution in every output mode. JSON and streaming
summaries describe the deployment outcome, including their `status` and `exit_code`.
A subsequent fatal local error is reported on stderr and makes the process exit
nonzero, without another stdout summary. Use the process exit status for the overall
command result. Error notifications for callbacks have `phase="local_execute"`.

See [Lifecycle Signals](signals.md) for error notifications and cancellation behavior.
