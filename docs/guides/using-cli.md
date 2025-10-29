# Using Stelvio CLI

The Stelvio CLI (`stlv`) manages your AWS infrastructure deployments.

## Global Options

- `--verbose, -v` - Show INFO level logs  
- `-vv` - Show DEBUG level logs
- `--help` - Show command help

## Commands

### `stlv init`

Initializes a new Stelvio project in the current directory.

```bash
stlv init
```

**Options:**

- `--profile YOUR_PROFILE_NAME` - AWS profile name
- `--region YOUR_REGION` - AWS region (e.g., us-east-1, eu-west-1)

Creates `stlv_app.py` with your project configuration. If you don't specify options, you'll be prompted for AWS profile and region.

### `stlv diff [ENVIRONMENT]`

Shows what changes will happen when you deploy. Uses your personal environment if none specified.

```bash
stlv diff
stlv diff staging
stlv diff prod
```

### `stlv deploy [ENVIRONMENT]`

Deploys your infrastructure to AWS. Uses your personal environment if none specified.

```bash
stlv deploy
stlv deploy staging
stlv deploy prod
```

**Options:**

- `--yes, -y` - Skip confirmation prompts

!!! warning
    Shared environments ask for confirmation unless you use `--yes`.

### `stlv refresh [ENVIRONMENT]`

Syncs your local state with what's actually running in AWS. Uses your personal environment if none specified.

```bash
stlv refresh
stlv refresh prod
```

Use this when someone else changed your infrastructure outside of Stelvio. It detects "drift" - differences between your code and what's actually deployed. If drift is found, you can either update your code to match reality or deploy to revert the changes.

### `stlv destroy [ENVIRONMENT]`

Destroys all infrastructure in an environment. Uses your personal environment if none specified.

```bash
stlv destroy staging
stlv destroy
```

**Options:**

- `--yes, -y` - Skip confirmation prompts

!!! danger
    This deletes everything. Always asks for confirmation unless you use `--yes`.

### `stlv unlock [ENVIRONMENT]`

Unlocks your Stelvio project when deployment state becomes locked. Uses your personal environment if none specified.

```bash
stlv unlock
stlv unlock staging
```

Use this when:
- A previous deployment was interrupted (Ctrl+C, network issue, etc.)
- You see "Stack is currently being updated" errors
- Pulumi state is locked and preventing new deployments

!!! warning
    Only run this if you're sure no other deployment is actually running. Running `unlock` while another deployment is active can cause state corruption.

### `stlv version` / `stlv --version`

Shows versions of Stelvio and Pulumi.

```bash
stlv version
stlv --version
```

## Environments

Stelvio uses environments to keep your deployments separate.

### Personal Environments

By default, commands use your username as the environment:

- You get your own sandbox to develop in
- No conflicts with teammates
- Safe to experiment without affecting others

### Shared Environments

Use explicit names for shared environments, e.g.:

- `staging` - For testing before production
- `prod` - Your live application
- `demo` - For client demonstrations

### Examples

```bash
# Personal development
stlv deploy                 # deploys to "john" environment
stlv diff                   # checks "john" environment

# Team environments  
stlv deploy staging         # deploys to shared staging
stlv deploy prod           # deploys to production
```

## Common Workflows

### Starting a new project

```bash
mkdir my-api && cd my-api
stlv init
# Edit stlv_app.py to define your infrastructure
stlv diff
stlv deploy
```

### Daily development

```bash
stlv diff              # see what changed
stlv deploy            # deploy to your environment
```

### Releasing to production

```bash
stlv diff prod         # review production changes
stlv deploy prod       # deploy with confirmation
```

### Cleaning up

```bash
stlv destroy           # remove your personal environment
stlv destroy staging   # remove staging environment
```

### Showing Pulumi outputs

```bash
stlv output            # Shows a list of Pulumi outputs
stlv output --json     # Shows a list of Pulumi outputs in JSON format
```

### System Check
```bash
stlv system            # Ensures stelvio can run properly
```

`stlv system` will install Pulumi, but does not act on any cloud resources. 
This comes in handy if used within a Docker file (to make sure the final image is as complete as possible).

## Need Help?

- Use `stlv COMMAND --help` for command details
- Use `-v` or `-vv` flags for more detailed error information