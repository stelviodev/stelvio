# StelvioApp and stlv_app.py

Every Stelvio project has a `stlv_app.py` file at its root - this is where you create your Stelvio application. The Stelvio CLI automatically looks for this file in your current directory.

## Minimal example

At its simplest, a `stlv_app.py` only needs a `StelvioApp` instance and an
`@app.run` function:

```python
from stelvio.app import StelvioApp
from stelvio.aws.function import Function

app = StelvioApp("my-app")


@app.run
def run() -> None:
    Function("process-user", handler="functions/users.process")
```

!!! warning
    The app name is used to identify your infrastructure state. Changing it creates
    new resources rather than renaming existing ones.
    See [State Management - Renaming](state.md#renaming) for details.

## Decorators

### @app.run (required)

This is where you define your infrastructure components. It runs after configuration is loaded.
All Stelvio components must be created inside this function (or in modules when using auto-discovery). See [Project Structure](../intro/project-structure.md) for details on component creation order.


### @app.config (optional)
For production use, you'll want to add an `@app.config` function. It configures Stelvio for your project and environment.

The function receives `env: str` - the environment name (e.g., "dev", "prod") and returns a `StelvioAppConfig` object. Stelvio calls it before any infrastructure is created.

Add it when you need to:

- Pin a specific AWS profile or region
- [Configure shared environments](../concepts/environments.md#configuring-environments) (e.g., `["staging", "prod"]`).
- [Set global tags](../concepts/tags.md#global-tags)
- [Configure DNS providers](../concepts/dns.md#configuring-a-dns-provider)
- [Apply global component customizations](../concepts/customization.md#global-customization)

!!! warning
    Without `@app.config`, only your personal environment (your username) is available. See [Configuring environments](../concepts/environments.md#configuring-environments).

## Configuring AWS credentials and region

Stelvio follows the same credential resolution as the AWS CLI.

It looks at (in this order):
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` env vars
- `AWS_PROFILE` env var
- The default profile in `~/.aws/credentials` / `~/.aws/config`

Profiles are entries in `~/.aws/config` / `~/.aws/credentials`. You can create them with `aws configure`, `aws sso login`, `aws configure sso`.

AWS region resolves similarly. It uses `AWS_REGION`, `AWS_DEFAULT_REGION` or if none set it uses the selected profile's default.

!!! warning "Environment variables must be exported"
    Just setting `AWS_PROFILE=my_profile` is not enough. It only sets a shell variable. Subprocesses like `stlv` will not see it. You need to use `export AWS_PROFILE=my_profile`. Verify by running `env | grep AWS`.

You can override this behaviour by using `profile` and `region` parameters of `AwsConfig`:

```python
from stelvio.config import AwsConfig, StelvioAppConfig
from stelvio.app import StelvioApp

app = StelvioApp("my-app")


@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(profile="my-profile", region="eu-west-1")
    )


@app.run
def run() -> None:
    ...
```

## Next Steps

Now that you understand the StelvioApp structure, you might want to explore:

- [Project Structure](../intro/project-structure.md) - Learn about organizing your project files
- [Environments](environments.md) - Understand environment management
- [Working with Lambda Functions](../components/aws/lambda.md) - Create your first Lambda function
- [Linking](linking.md) - Connect resources with automatic IAM management