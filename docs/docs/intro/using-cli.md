# Using Stelvio CLI

The Stelvio CLI (`stelvio`) manages your AWS infrastructure deployments.

## Global Options

- `--verbose, -v` - Show INFO level logs
- `-vv` - Show DEBUG level logs
- `--help` - Show command help

Global options go right after `stelvio`:

```bash
stelvio -v deploy staging
stelvio -vv diff
```

## Commands

### init

Initializes a new Stelvio project in the current directory.

```bash
stelvio init
```

**Options:**

- `--profile YOUR_PROFILE_NAME` - AWS profile name
- `--region YOUR_REGION` - AWS region (e.g., us-east-1, eu-west-1)

Creates `stelvio_app.py` with your project configuration. If you don't specify options, you'll be prompted for AWS profile and region.

### diff

`stelvio diff [env]` - Shows what changes will happen for specified environment. Defaults to personal environment if not provided.

```bash
stelvio diff
stelvio diff staging
```

### deploy

`stelvio deploy [env]` - Deploys your infrastructure to specified environment. Defaults to personal environment if not provided.

```bash
stelvio deploy
stelvio deploy staging
```

**Options:**

- `--yes, -y` - Skip confirmation prompts

!!! warning
    Shared environments ask for confirmation unless you use `--yes`.

### refresh

`stelvio refresh [env]` - Updates your state to match what's actually in AWS for specified environment. Defaults to personal environment if not provided.

```bash
stelvio refresh
stelvio refresh prod
```

Use this when resources were changed outside of Stelvio (e.g., someone modified a Lambda in the AWS console). Refresh updates your state to match what's actually in AWS.

After refreshing, run `stelvio diff` to see the difference between your code and the updated state. You can then either:

- Update your code to match the changes made in AWS
- Run `stelvio deploy` to revert AWS back to what your code defines

**What refresh does:**

- Updates state for resources already tracked by Stelvio
- Detects drift (differences between state and actual AWS resources)

**What refresh does NOT do:**

- Import resources that exist in AWS but aren't in state
- Modify your code or infrastructure definition
- Create, update, or delete any AWS resources

### destroy

`stelvio destroy [env]` - Destroys all infrastructure in specified environment. Defaults to personal environment if not provided.

```bash
stelvio destroy
stelvio destroy staging
```

**Options:**

- `--yes, -y` - Skip confirmation prompts

!!! danger
    This deletes everything. Always asks for confirmation unless you use `--yes`.

### unlock

`stelvio unlock [env]` - Unlocks state when a previous operation was interrupted. Defaults to personal environment if not provided.

```bash
stelvio unlock
stelvio unlock staging
```

Use this when:

- A previous deployment was interrupted (Ctrl+C, network issue, etc.)
- You see "Stack is currently being updated" errors

!!! warning
    Only run this if you're sure no other deployment is actually running. Running `unlock` while another deployment is active can cause state corruption.

### outputs

`stelvio outputs [env]` - Shows stack outputs for specified environment. Defaults to personal environment if not provided.

```bash
stelvio outputs
stelvio outputs staging
stelvio outputs --json
```

**Options:**

- `--json` - Output as JSON for scripting

### state

Manage infrastructure state directly. Use for recovery scenarios.

#### state list

`stelvio state list [-e env]` - Lists all resources tracked in state. Use `-e/--env` to specify environment. Defaults to personal environment if not provided.

```bash
stelvio state list
stelvio state list -e prod
```

#### state rm

`stelvio state rm <resource> [-e env]` - Removes a resource from state without deleting from AWS. Use `-e/--env` to specify environment. Defaults to personal environment if not provided.

```bash
stelvio state rm my-function
stelvio state rm my-function -e staging
```

Use when you've manually deleted something in AWS and need to clean up state.

!!! warning
    This only removes resource from state. The resource may still exist in AWS.

#### state repair

`stelvio state repair [-e env]` - Repairs corrupted state by fixing orphans and broken dependencies. Use `-e/--env` to specify environment. Defaults to personal environment if not provided.

```bash
stelvio state repair
stelvio state repair -e staging
```

Use after manual state edits or when Pulumi complains about missing resources.

### system

Checks system requirements and installs Pulumi if needed.

```bash
stelvio system
```

Useful in Dockerfiles to ensure the image is ready for deployments.

### version

Shows versions of Stelvio and Pulumi.

```bash
stelvio version
stelvio --version
```

## Environments

Most commands accept an optional environment name. Without one, commands use your personal environment (your username by default).

See [Environments](../concepts/environments.md) for details on personal vs shared environments and configuration options.

## Need Help?

- Use `stelvio COMMAND --help` for command details
- Use `-v` or `-vv` flags for more detailed error information
