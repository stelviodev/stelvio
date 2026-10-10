# Lifecycle signals

Use `@app.on` in `stlv_app.py` to run Python code around deployment and development
operations. A signal can have multiple handlers. Stelvio calls them sequentially in
registration order and waits for each to finish, including `async def` handlers.

```python
import sys

from stelvio.app import StelvioApp
from stelvio import signals
from stelvio.signals import DeployEvent

app = StelvioApp("my-app")


@app.run
def run() -> None:
    pass  # Define your components here.


@app.on(signals.after_deploy, errors="continue")
def report_deployment(event: DeployEvent) -> None:
    if event.dev_mode:
        return
    print(f"Deployed {event.app_name} to {event.env}", file=sys.stderr)
```

Define handlers at module level, or import their registration modules from
`stlv_app.py`. Modules discovered during infrastructure creation are too late to
receive the current operation's before signal. Registering the same function for
the same signal twice keeps the first registration and its error policy.

## Operation signals

| Before | After | Event type |
|--------|-------|------------|
| `before_deploy` | `after_deploy` | `DeployEvent` |
| `before_diff` | `after_diff` | `DiffEvent` |
| `before_destroy` | `after_destroy` | `DestroyEvent` |
| `before_refresh` | `after_refresh` | `RefreshEvent` |
| `before_dev` | `after_dev` | `DevEvent` |
| `before_dev_bridge_start` | `after_dev_bridge_start` | `DevBridgeEvent` |
| `before_dev_bridge_stop` | `after_dev_bridge_stop` | `DevBridgeEvent` |

All identifiers are available from `stelvio.signals`. Event types are imported
from the same module.

Before-operation handlers run after `stlv_app.py` and `@app.config` have loaded,
before backend setup consumes configuration. Final configuration is validated,
and required confirmation still happens before cloud mutations. Destroy confirmation
also precedes backend setup, even when nothing has been deployed yet.

After-operation signals fire only on success, after persistence and cleanup.
`after_deploy` guarantees provisioning, final state saving, snapshot creation,
completion recording, lock release, and local cleanup succeeded. A failing
after-handler still fails the command by default; it does not undo the deployment.
Stelvio prints the final success summary after the after-handlers complete.

Destroy and refresh emit their operation signals when nothing is deployed, with
`event.result.no_op` set to `True`. Declined confirmation emits `on_cancel` instead
of an after signal.

## Payloads

Events have immutable identity fields and typed properties. Results are captured
before temporary state files are removed.

| Field | Meaning |
|-------|---------|
| `signal` | Built-in signal identifier, with a `.name` |
| `app_name`, `env` | App and selected environment |
| `command` | Originating command: `deploy`, `diff`, `destroy`, `refresh`, or `dev` |
| `operation` | Current operation; `deploy` during the deployment inside dev |
| `dev_mode` | Whether the originating command is `dev` |
| `config` | Configuration snapshot; unavailable if configuration failed |
| `options` | Immutable `OperationOptions` snapshot |
| `update_id` | Update identifier once setup has assigned it |
| `result` | `OperationResult` on completion, otherwise `None` |
| `outputs` | Captured deploy outputs in the [CLI output format](../intro/using-cli.md), otherwise `{}` |

`OperationResult` contains `no_op` and `outputs`. Results on error events may be
available when infrastructure completed but an after-handler or cleanup failed.

## Editing operation inputs

Before-operation handlers can explicitly replace configuration or change supported
options. Later handlers see accepted changes. Changing a returned configuration
snapshot does not update Stelvio; use `replace_config`.

```python
from dataclasses import replace

from stelvio import signals
from stelvio.config import AwsConfig
from stelvio.signals import DeployEvent


@app.on(signals.before_deploy)
def configure_deployment(event: DeployEvent) -> None:
    assert event.config is not None
    event.replace_config(
        replace(event.config, aws=AwsConfig(region="eu-west-1"))
    )
    event.update_options(show_unchanged=True)
```

| Method or option | Supported operations |
|------------------|----------------------|
| `replace_config(StelvioAppConfig(...))` | Deploy, diff, destroy, refresh, dev |
| `update_options(show_unchanged=True)` | Deploy, diff, dev |
| `update_options(compact=True)` | Diff |

Input editing is available only while before-operation handlers are executing.
Retained events reject edits afterward. App/environment identity, confirmation
requirements, JSON/stream mode, and provisioning options are not editable.
Handler return values are ignored.

## Errors and cancellation

Handlers may run arbitrary Python code, including migrations, subprocesses, and
API calls. Choose how Stelvio handles exceptions from each registration:

| `errors` | Behavior |
|----------|----------|
| `"fail"` (default) | Stop normal execution and remaining ordinary handlers; run required recovery and cleanup; fail the command |
| `"continue"` | Report the exception to stderr, then run remaining handlers and continue |

Raise an exception from a before-handler using the default policy to reject the
operation. Keyboard interruption and async cancellation remain cancellation even
with `errors="continue"`.

```python
import sys

from stelvio import signals
from stelvio.signals import ErrorEvent, CancelEvent


@app.on(signals.on_error)
def report_failure(event: ErrorEvent) -> None:
    print(f"{event.operation} failed during {event.phase}: {event.error}", file=sys.stderr)


@app.on(signals.on_cancel)
def report_cancellation(event: CancelEvent) -> None:
    print(f"{event.operation} cancelled: {event.reason}", file=sys.stderr)
```

`ErrorEvent` adds `error` (the original exception), `phase` (diagnostic location),
and `handler` (the failing handler's name, when applicable). `on_error` runs once
for the underlying operation failure, including configuration failures after
registration, setup, state handling, and fatal handler exceptions. It does not
report failed individual Lambda invocations or exceptions handled with `"continue"`.
Diagnostic phase strings are locations, not additional subscribable signals.

`CancelEvent` adds `phase` and a `reason`: `"interrupted"` or
`"confirmation_declined"`. Ctrl+C suppresses success signals and still runs
required cleanup. Declining confirmation keeps the existing successful CLI exit;
interruption retains the CLI's interruption behavior.

All error and cancellation handlers run even if one fails. Their secondary errors
go to stderr, preserve the original outcome, and never recursively emit `on_error`.
Required cleanup cannot be vetoed by a failing shutdown handler.

!!! note "Handler output"
    Stelvio's handler diagnostics go to stderr. Your handlers control their own
    output. Use stderr or an external destination for logs when running with
    `--json` or `--stream`, so stdout remains machine-readable.

## Dev sessions

A normal dev session emits:

1. `before_dev`
2. `before_deploy`
3. `after_deploy`, after deployment persistence and cleanup
4. `before_dev_bridge_start`
5. `after_dev_bridge_start`, after connection and subscription confirmation
6. `before_dev_bridge_stop`
7. `after_dev_bridge_stop`, after closing the connection
8. `after_dev`, when the session completes normally

All these events have `command="dev"` and `dev_mode=True`. Deploy events have
`operation="deploy"`; the remaining events have `operation="dev"`. Both initial
before-operation signals can edit inputs before setup consumes them.

`DevBridgeEvent.ready` indicates whether this bridge reached readiness. Bridge
handlers run in the bridge's existing async event loop. On Ctrl+C, shutdown signals
run for an open connection, followed by `on_cancel`; `after_dev` does not fire.
If subscription fails, the connection is closed, shutdown handlers run, and
`on_error` fires without a bridge-ready or successful dev-completion event.

## Boundaries

Registration must succeed before Stelvio can notify handlers. Process termination
or a second forced interruption can prevent cleanup or delivery. Handlers must
finish before the operation can proceed; Stelvio does not retry or impose a timeout.
Handlers manage the lifetime of any background work they start.

This release provides operation signals and dev bridge boundaries. Individual phase
signals and infrastructure-editing hooks are deferred. Existing component creation
boundaries remain in place; already registered components cannot be removed by a
signal. Internal stack and registry objects are not exposed, and custom signals
cannot be registered.
