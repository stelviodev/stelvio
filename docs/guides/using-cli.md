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

**Options:**

- `--profile YOUR_PROFILE_NAME` - AWS profile name
- `--region YOUR_REGION` - AWS region (e.g., us-east-1, eu-west-1)

Creates `stlv_app.py` with your project configuration. If you don't specify options, you'll be prompted for AWS profile and region.

### diff

`stlv diff [env]` - Shows what changes will happen for specified environment. Defaults to personal environment if not provided.

```bash
stlv diff
stlv diff staging
```

### deploy

`stlv deploy [env]` - Deploys your infrastructure to specified environment. Defaults to personal environment if not provided.

```bash
stlv deploy
stlv deploy staging
```

**Options:**

- `--yes, -y` - Skip confirmation prompts

!!! warning
    Shared environments ask for confirmation unless you use `--yes`.

### refresh

`stlv refresh [env]` - Updates your state to match what's actually in AWS for specified environment. Defaults to personal environment if not provided.

```bash
stlv refresh
stlv refresh prod
```

Use this when resources were changed outside of Stelvio (e.g., someone modified a Lambda in the AWS console). Refresh updates your state to match what's actually in AWS.

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
```

**Options:**

- `--yes, -y` - Skip confirmation prompts

!!! danger
    This deletes everything. Always asks for confirmation unless you use `--yes`.

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

`stlv outputs [env]` - Shows stack outputs for specified environment. Defaults to personal environment if not provided.

```bash
stlv outputs
stlv outputs staging
stlv outputs --json
```

**Options:**

- `--json` - Output as JSON for scripting

### state

Manage infrastructure state directly. Use for recovery scenarios.

#### state list

`stlv state list [-e env]` - Lists all resources tracked in state. Use `-e/--env` to specify environment. Defaults to personal environment if not provided.

```bash
stlv state list
stlv state list -e prod
```

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

## Environments

Most commands accept an optional environment name. Without one, commands use your personal environment (your username by default).

See [Environments](environments.md) for details on personal vs shared environments and configuration options.

## Need Help?

- Use `stlv COMMAND --help` for command details
- Use `-v` or `-vv` flags for more detailed error information
