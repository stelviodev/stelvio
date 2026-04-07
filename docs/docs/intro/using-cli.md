# Using Stelvio CLI

The Stelvio CLI (`stlv`) manages your AWS infrastructure deployments.

## Global Options

- `--verbose, -v` - Show INFO level logs
- `-vv` - Show DEBUG level logs
- `--help` - Show command help

Global options go right after `stlv`:

```bash
stlv -v deploy staging
stlv -vv diff
```

## Commands

### init

Initializes a new Stelvio project in the current directory.

```bash
stlv init
```

Creates `stlv_app.py` with a starter configuration template.

### diff

`stlv diff [env]` - Shows what changes will happen for specified environment. Defaults to personal environment if not provided.

```bash
stlv diff
stlv diff staging
stlv diff --json
```

**Options:**

- `--json` - Output a final JSON summary only (no Rich header/spinner output)

`diff` is the normal way to review infrastructure changes before deploying them.
Use `--json` when you want a single machine-readable summary at the end.
`diff` does not support `--stream`.

### deploy

`stlv deploy [env]` - Deploys your infrastructure to specified environment. Defaults to personal environment if not provided.

```bash
stlv deploy
stlv deploy staging
stlv deploy staging --yes --json
stlv deploy staging --yes --stream
```

**Options:**

- `--yes, -y` - Skip confirmation prompts
- `--json` - Output a final JSON summary only (no Rich header/spinner output)
- `--stream` - Output newline-delimited JSON events during the operation

Human-readable deploy output shows changed components as they finish, then prints component URLs and any user-defined exports.

`--stream` is intended for agents and scripts that want live machine-readable progress. The stream emits:

- `start`
- `resource` when a changed resource finishes
- `warning`
- `error`
- final `summary`

!!! warning
    Shared environments ask for confirmation unless you use `--yes`.
    In JSON mode, Stelvio never prompts. `stlv deploy ENV --json` therefore requires `--yes`
    for shared environments. `stlv deploy ENV --stream` follows the same rule.
    Outside CI, commands keep the existing default of using your personal environment when env is omitted.

### refresh

`stlv refresh [env]` - Updates your state to match what's actually in AWS for specified environment. Defaults to personal environment if not provided.

```bash
stlv refresh
stlv refresh prod
stlv refresh prod --json
```

**Options:**

- `--json` - Output a final JSON summary only (no Rich header/spinner output)

Use this when resources were changed outside of Stelvio (for example, in the AWS console) and you need Pulumi state to catch up with reality.

Normal day-to-day workflow is still:

- `stlv diff`
- `stlv deploy`

`refresh` is a recovery and reconciliation command, not a normal replacement for `diff`.
`refresh` does not support `--stream`.

After refreshing, run `stlv diff` to see the difference between your code and the updated state. You can then either:

- Update your code to match the changes made in AWS
- Run `stlv deploy` to revert AWS back to what your code defines

**What refresh does:**

- Updates state for resources already tracked by Stelvio
- Detects drift (differences between state and actual AWS resources)

**What refresh does NOT do:**

- Import resources that exist in AWS but aren't in state
- Modify your code or infrastructure definition
- Create, update, or delete any AWS resources

### destroy

`stlv destroy [env]` - Destroys all infrastructure in specified environment. Defaults to personal environment if not provided.

```bash
stlv destroy
stlv destroy staging
stlv destroy staging --yes --json
stlv destroy staging --yes --stream
```

**Options:**

- `--yes, -y` - Skip confirmation prompts
- `--json` - Output a final JSON summary only (no Rich header/spinner output)
- `--stream` - Output newline-delimited JSON events during the operation

`--stream` uses the same event contract as `deploy --stream`:

- `start`
- `resource` when a changed resource finishes
- `warning`
- `error`
- final `summary`

!!! danger
    This deletes everything. Always asks for confirmation unless you use `--yes`.
    In JSON mode, Stelvio never prompts. `stlv destroy --json` therefore always requires
    `--yes`. `stlv destroy --stream` follows the same rule.

### unlock

`stlv unlock [env]` - Unlocks state when a previous operation was interrupted. Defaults to personal environment if not provided.

```bash
stlv unlock
stlv unlock staging
```

Use this when:

- A previous deployment was interrupted (Ctrl+C, network issue, etc.)
- You see "Stack is currently being updated" errors

!!! warning
    Only run this if you're sure no other deployment is actually running. Running `unlock` while another deployment is active can cause state corruption.

### outputs

`stlv outputs [env]` - Shows component URLs and user-defined exports. Defaults to your personal environment if not provided.

```bash
stlv outputs
stlv outputs staging
stlv outputs --json
```

**Options:**

- `--json` - Output as JSON for scripting

Only components with a URL/endpoint (Api, AppSync, CloudFront, Router, S3StaticWebsite) display a value. User-defined exports (via `export_output`) are shown in a separate section.

To export custom values, use `export_output` in your `stlv_app.py`:

```python
from stelvio import export_output

export_output("api_url", api.resources.stage.invoke_url)
```

### state

Manage infrastructure state directly. Use for recovery scenarios.

#### state list

`stlv state list [-e env] [--json] [--outputs]` - Lists all resources tracked in state. Human output is grouped under the Pulumi stack root and Stelvio components. Use `-e/--env` to specify environment. Defaults to personal environment if not provided.

```bash
stlv state list
stlv state list -e prod
stlv state list --json
stlv state list --outputs
```

**Options:**

- `--json` - Output as JSON
- `--outputs` - Show Pulumi outputs stored per resource (debugging)

**Output shape:**

Human mode shows:

- `Stack <name>` at the top
- Stelvio components nested below it
- `Providers` in a separate section
- `Depends on:` for resource dependencies when present
- With `--outputs`: raw output values stored in state per resource

`--json` returns structured state data with:

- `stack`
- `components`
- `providers`
- optional `other_roots`

#### state rm

`stlv state rm <resource> [-e env]` - Removes a resource from state without deleting from AWS. Use `-e/--env` to specify environment. Defaults to personal environment if not provided.

```bash
stlv state rm my-function
stlv state rm my-function -e staging
```

Use when you've manually deleted something in AWS and need to clean up state.

!!! warning
    This only removes resource from state. The resource may still exist in AWS.

#### state repair

`stlv state repair [-e env]` - Repairs corrupted state by fixing orphans and broken dependencies. Use `-e/--env` to specify environment. Defaults to personal environment if not provided.

```bash
stlv state repair
stlv state repair -e staging
```

Use after manual state edits or when Pulumi complains about missing resources.

### system

Checks system requirements and installs Pulumi if needed.

```bash
stlv system
```

Useful in Dockerfiles to ensure the image is ready for deployments.

### version

Shows versions of Stelvio and Pulumi.

```bash
stlv version
stlv --version
```

### exit codes

Stelvio currently uses these stable exit codes for automation:

- `0` - success
- `1` - operation/runtime failure
- `2` - usage, project, or environment validation error
- `4` - state locked by another operation

## Environments

Most commands accept an optional environment name. Without one, commands use your personal environment (your username by default).

!!! warning
    In CI, Stelvio requires an explicit environment for `diff`, `deploy`, `dev`, `refresh`,
    and `destroy`. For example: `stlv deploy prod`.
    Outside CI, commands keep the existing default of using your personal environment when env is omitted.

See [Environments](../concepts/environments.md) for details on personal vs shared environments and configuration options.

## Need Help?

- Use `stlv COMMAND --help` for command details
- Use `-v` or `-vv` flags for more detailed error information
