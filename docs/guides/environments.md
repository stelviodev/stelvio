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

If you don't specify an environment, Stelvio uses your computer username:

```bash
stlv deploy  # Deploys to your personal environment (e.g., "john")
```

This gives every developer their own isolated sandbox to work in without
conflicts.

### Customizing Your Personal Environment Name

Stelvio stores your personal environment name in `.stelvio/userenv`. You can customize this if needed:

```bash
# Create custom personal environment name
echo "myname" > .stelvio/userenv

# Now deployments use "myname" instead of your computer username
stlv deploy  # Deploys to "myname" environment
```

This is useful when:

- Multiple developers share computers or usernames
- You want a consistent environment name across different machines
- Your computer username contains special characters

!!! info "File Location"
    The `.stelvio/userenv` file is project-specific and should be added to `.gitignore` since it's personal to each developer.

## Environment Commands

### Deploy to Different Environments

```bash
# Personal environment (default)
stlv deploy

# Staging environment
stlv deploy staging

# Production environment
stlv deploy prod
```

### Preview Changes by Environment

```bash
# See what would change in staging
stlv diff staging

# See what would change in your personal environment
stlv diff
```

### Destroy Environment Resources

```bash
# Destroy your personal environment
stlv destroy

# Destroy staging (be careful!)
stlv destroy staging
```


## Configuring Environments

You can define which environments are valid for your project:

```python
from stelvio.config import StelvioAppConfig, AwsConfig


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(region="us-east-1"),
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
@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            region="us-east-1",
            profile="production" if env == "prod" else "default"
        ),
        environments=["staging", "prod"]
    )
```

## Best Practices

### Personal Development

- Use your personal environment (default) for active development
- Experiment freely - it's isolated from others
- Clean up regularly with `stlv destroy` when done with features

### Shared Environments

- Use confirmation prompts for shared environments (Stelvio asks automatically)
- Document environment purposes in your team
- Consider using different AWS regions or accounts for production

### Naming Conventions

- Keep environment names short and clear: `dev`, `stag`, `prod`
- Avoid special characters - stick to letters and numbers
- Be consistent across projects
