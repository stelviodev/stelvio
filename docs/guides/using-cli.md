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

### `stlv diff`

Shows what changes will happen when you deploy.

```bash
stlv diff
stlv diff staging
stlv diff prod
```

**Default environment:** Your username (e.g., `john`)

### `stlv deploy`

Deploys your infrastructure to AWS.

```bash
stlv deploy
stlv deploy staging
stlv deploy prod
```

**Options:**

- `--yes, -y` - Skip confirmation prompts

**Default environment:** Your username (e.g., `john`)

!!! warning
    Shared environments ask for confirmation unless you use `--yes`.

### `stlv refresh`

Syncs your local state with what's actually running in AWS.

```bash
stlv refresh
stlv refresh prod
```

**Default environment:** Your username (e.g., `john`)

Use this when someone else changed your infrastructure outside of Stelvio. It detects "drift" - differences between your code and what's actually deployed. If drift is found, you can either update your code to match reality or deploy to revert the changes.

### `stlv destroy`

Destroys all infrastructure in an environment.

```bash
stlv destroy staging
stlv destroy
```

**Options:**

- `--yes, -y` - Skip confirmation prompts

**Default environment:** Your username (e.g., `john`)

!!! danger
    This deletes everything. Always asks for confirmation unless you use `--yes`.

### `stlv version`

Shows your Stelvio version.

```bash
stlv version
```

## Environment Management

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

## Need Help?

- Use `stlv COMMAND --help` for command details
- Use `-v` or `-vv` flags for more detailed error information