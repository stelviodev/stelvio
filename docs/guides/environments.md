# Environments

Stelvio makes it easy to manage different deployment environments like
development, staging, and production. Each environment gets its own isolated AWS
resources with automatic naming to prevent conflicts.

## How Environments Work

When you deploy with Stelvio, every AWS resource gets named with this pattern:

```
{app-name}-{environment}-{resource-name}
```

For example, if your app is called "my-api" and you deploy to the "staging"
environment, a DynamoDB table named "users" becomes `my-api-staging-users`.

## Default Behavior

If you don't specify an environment, Stelvio uses your personal environment (computer username by default):

```bash
stlv deploy  # Deploys to your personal environment (e.g., "john")
```

This gives every developer their own isolated sandbox to work in without
conflicts.

### Customizing Your Personal Environment Name

Stelvio stores your personal environment name in `.stelvio/userenv`. You can customize this if needed:

```bash
echo "myname" > .stelvio/userenv
```

This is useful when:

- Multiple developers on the team have the same computer username
- You want a consistent name across different machines
- Your computer username contains special characters
- You want to use something other than your computer username

!!! info
    The `.stelvio/` folder contains personal settings and caches - add it to `.gitignore`.

## Using Environments

Most CLI commands accept an optional environment name as an argument:

```bash
stlv deploy              # Your personal environment
stlv deploy staging      # Staging environment
stlv deploy prod         # Production environment
```

Without an environment argument, commands default to your personal environment. See [Using CLI](using-cli.md) for the full list of commands.

## Configuring Environments

You can define which environments are valid for your project:

```python
from stelvio.config import StelvioAppConfig


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        environments=["staging", "prod"]  # Valid shared environments
    )
```

With this configuration:

- **Anyone** can deploy to their personal environment (username)
- **Only** "staging" and "prod" are accepted as shared environments
- Stelvio will validate environment names and show an error for invalid ones

## Environment-Specific Configuration

You can customize settings per environment:

```python
from stelvio.config import AwsConfig

@app.config
def configuration(env: str) -> StelvioAppConfig:
    if env == "prod":
        return StelvioAppConfig(
            aws=AwsConfig(profile="production-account"),
            environments=["staging", "prod"]
        )
    return StelvioAppConfig(environments=["staging", "prod"])
```

## Tips

- Keep environment names short: `dev`, `staging`, `prod`
- Avoid special characters - stick to letters and numbers
- Consider using different AWS accounts for production

## Resource Naming

### Naming Pattern

All AWS resources follow the `{app}-{env}-{name}` pattern. Some resources have additional suffixes to identify their type:

| Resource | Pattern |
|----------|---------|
| IAM Roles | `{app}-{env}-{name}-r` |
| IAM Policies | `{app}-{env}-{name}-p`|

### Automatic Truncation

When a name would exceed AWS limits, Stelvio automatically truncates it and adds a 7-character hash to keep it unique:

```text
# This name is too long for the 64-char IAM role limit
myapp-prod-process-user-authentication-requests-handler-r

# Stelvio truncates and adds a hash for uniqueness
myapp-prod-process-user-authentication-request-e4f2a91-r
```

The hash is derived from the original name, so:

- The same name always produces the same truncated result
- Different long names won't collide even if they start the same way
- You can still identify the resource from the readable portion
