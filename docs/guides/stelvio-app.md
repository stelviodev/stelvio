# StelvioApp and stlv_app.py

Every Stelvio project has a `stlv_app.py` file at its root - this is the
cornerstone of your infrastructure definition. The CLI automatically looks for
this file in your current directory.

## Basic Structure

```python
from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig

from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.function import Function

# Create your app instance
app = StelvioApp("my-project-name")


# Configuration function - runs first
@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            region="us-east-1",
            profile="default"  # or None for environment variables
        )
    )


# Infrastructure definition - runs after configuration
@app.run
def run() -> None:
    # Create your AWS resources here
    table = DynamoTable(name="users", ...)
    fn = Function(handler="functions/users.process", links=[table])
```

## The Two Required Decorators

For Stelvio to load and work properly you need to create `StelvioApp` object 
with  
`app = StelvioApp("some-name")` and then create two functions. 

One which 
will have `@app.config` decorator and one with `@app.run` decorator.

### @app.config

- **Purpose**: Configures Stelvio for your project and environment
- **Parameters**: `env: str` - the environment name (e.g., "dev", "staging", "
  prod")
- **Returns**: `StelvioAppConfig` object with AWS settings and other
  configuration
- **Timing**: Runs first, before any infrastructure is created

### @app.run

- **Purpose**: Defines your infrastructure components
- **Timing**: Runs after configuration is loaded
- **Requirement**: All Stelvio components must be created inside this function (
  or in modules when using auto-discovery). See [Project Structure](project-structure.md) for details on component creation order.

## Environment-Specific Configuration

You can customize settings based on the environment:

```python
@app.config
def configuration(env: str) -> StelvioAppConfig:
    # Different AWS profiles per environment
    aws_profile = "production" if env == "prod" else "default"

    return StelvioAppConfig(
        aws=AwsConfig(
            region="us-east-1",
            profile=aws_profile
        ),
        environments=["staging", "prod"]  # Valid shared environments
    )
```

## StelvioApp Class

The `StelvioApp` class is the main entry point for your infrastructure
definition and also defines name of your application.

```python
app = StelvioApp("my-project-name")
```

## Configuration Options

Function marked with `@app.config` decorator must return `StelvioAppConfig` object.
It supports several configuration options:

### AWS Configuration

```python
from stelvio.config import AwsConfig


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            region="us-east-1",  # AWS region
            profile="default",  # AWS profile name
        )
    )
```

If you don't set region or profile Stelvio will use `AWS_` envars or default AWS CLI configuration. 

### Environment Validation

```python
@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(region="us-east-1"),
        environments=["staging", "prod"]
        # Only these shared environments allowed
    )
```

With this configuration:

- **Anyone** can deploy to their personal environment (username)
- **Only** "staging" and "prod" are accepted as shared environments
- Stelvio will validate environment names and show an error for invalid ones



## Common Patterns

### Environment-Specific Resources

```python
from stelvio.app import context

...

@app.run
def run() -> None:
    env = context().environment

    if env == "prod":
        # Production-specific resources
        prod_only_lambda = Function(name="backup")

    # Common resources for all environments
    main_table = DynamoTable(name="users", ...)
```

## Next Steps

Now that you understand the StelvioApp structure, you might want to explore:

- [Project Structure](project-structure.md) - Learn about organizing your project files
- [Environments](environments.md) - Understand environment management
- [Working with Lambda Functions](lambda.md) - Create your first Lambda function
- [Linking](linking.md) - Connect resources with automatic IAM management
- 