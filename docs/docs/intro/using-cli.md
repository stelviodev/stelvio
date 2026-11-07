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

To start from a GitHub template instead:

```bash
stlv init --template gh:YOUR_ORG/app-templates@v1.0.0/http-service
```

See [Custom templates](../concepts/custom-templates.md).

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

### dev

`stlv dev [env]` deploys the local-handler bridge and starts the local server. For
Functions that depend on enabled VPC resources, it starts managed networking before
the bridge accepts invocations. It rejects dependent invocations until each required
VPC is ready. Functions without enabled VPC dependencies can continue independently.

```bash
stlv dev
stlv dev staging
```

Press `Ctrl+C` to stop. Stelvio closes the networking session and AppSync connection;
the deployed bastions remain available for later dev or standalone sessions.

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

export_output("api_url", api.url)
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

### Managed networking downloads

When a Stelvio networking operation requests its executables, Stelvio uses the
checksum-pinned Traforo `v0.0.7` release and AWS Session Manager plugin `1.2.835.0`.
The releases support macOS `arm64` and `x86_64`. Stelvio checks each artifact's
checksum, executable architecture, file permissions, and version before use.
Cache directories and developer images must also pass ownership and ACL checks.
Read-only grants and deny entries are allowed, including macOS files that have
both extended attributes and ACLs. Write grants are rejected. Do not change host
ACLs to bypass a validation failure.

If the Traforo release is private, set `GITHUB_TOKEN` in the environment with read
access to the release repository. Public release downloads do not need a token.
Stelvio reads the token from the environment and does not send it to a redirected
asset host. Do not put the token in a URL or commit it to project files.

For local Traforo development, set `STLV_TRAFORO_DEV_BINARY` to an absolute path to
the executable. Stelvio accepts the pinned release version or its matching
development snapshot version. An unsafe or incompatible override fails; Stelvio
does not fall back to the release or change its pinned cache.

### Native helper administration

Managed networking uses a privileged macOS helper. Install it with:

```bash
stlv tunnel install
```

The command verifies the selected Traforo image and Session Manager plugin before
it requests administrator authorization. It installs the helper and registers its
LaunchDaemon. During a networking session, the helper owns temporary VPC routes,
interfaces, and scoped resolver files.

Use these commands to check or recover the helper:

```bash
stlv tunnel status
stlv tunnel cleanup
stlv tunnel uninstall
```

`status` reports local helper ownership and VPC unit state; it does not verify AWS
or database connectivity. `cleanup` reconciles stale owned network state and
leaves the helper installed. `uninstall` removes the helper after
it confirms that no active session owns host state. It uses the installed image and
does not need a plugin download. If ownership is uncertain, the helper retains its
ownership evidence for status checks; do not remove resolver files or helper state by hand.

### Standalone access to deployed VPC resources

After deploying an app with enabled VPC dependencies and installing the helper, start
a foreground networking session without entering dev mode:

```bash
stlv tunnel start          # personal environment
stlv tunnel start staging  # named environment
```

The command reads the existing deployment, then releases its state context before it
waits for networking readiness. It does not deploy the app, start the AppSync bridge,
or create a bastion. It uses the same per-VPC discovery, DNS, readiness, and cleanup
rules as `stlv dev`. Press `Ctrl+C` to close the session; the deployment and its
bastion remain in place. If the app has no enabled VPC dependencies, the command
reports that networking is unnecessary and exits successfully.

Only VPCs linked by deployed handler dependencies are selected. Each selected VPC
routes its deployed private and isolated subnet CIDRs. Public subnets and unused
parts of the VPC block stay on the existing local network path. The VPC resolver
uses a separate SOCKS path; its address need not be inside a routed subnet.
Standalone access keeps the same production hostnames, ports, and TLS checks.

A route conflict is a terminal startup error. The command prints the configure
stage, requested and conflicting ranges, and interface index when supplied by the
helper, then closes the session and exits with an error. If cleanup is uncertain,
the route diagnostic and ownership evidence remain available. Resolve the LAN or
VPN overlap, check `stlv tunnel status`, and retry owned cleanup with
`stlv tunnel cleanup` before restarting. No whole-VPC fallback is attempted.

SSH host trust is shared with `stlv dev` and persists after `Ctrl+C`, command
restart, and helper uninstall. Restarting `tunnel start` does not accept a changed
host key. A mismatch exits with an error that names the instance and shows the
expected and presented fingerprints.

First investigate the change. Restore the original key when the change was not
intentional. For an authorized rotation, stop all tunnels for that instance and
reset only its trust entry:

```bash
stlv tunnel trust reset --partition aws --account 123456789012 \
  --region us-east-1 --instance-id i-0123456789abcdef0
stlv tunnel start staging
```

Reset shows the stored fingerprint and asks for confirmation. It does not change
AWS resources or install the helper. The next successful SSH connection establishes
a new pin. A replacement instance has a new ID and establishes its own pin.

### Route conflicts

A helper configuration can contain 1–32 selected IPv4 destination ranges. The
helper refuses an overlap with a non-default route on another interface. A LAN
range outside the selected subnets does not conflict just because it is inside
the same VPC block.

A route conflict reports the requested range, the existing route, and its
interface index. For example:

```text
requested route 10.254.20.0/22 conflicts with route 10.254.21.0/24 on interface index 7
```

Use this information to identify an overlapping LAN or VPN and choose a network
configuration that avoids the overlap. The error remains an uncertain helper
failure; it does not authorize removal of another application's routes. Check
`stlv tunnel status` and retain ownership evidence until cleanup is confirmed.
Do not delete helper journals or foreign routes to bypass the check.

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
