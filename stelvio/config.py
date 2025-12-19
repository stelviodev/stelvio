from dataclasses import dataclass, field
from typing import Literal

from stelvio.dns import Dns


@dataclass(frozen=True, kw_only=True)
class AwsConfig:
    """AWS configuration for Stelvio.

    Both profile and region are optional overrides. When not specified, Stelvio follows
    the standard AWS credential and region resolution chain.

    ## Credentials Resolution Order

    Stelvio uses boto3 and Pulumi, both of which follow the AWS SDK credential chain:

    1. **Environment variables**:
       - AWS_ACCESS_KEY_ID
       - AWS_SECRET_ACCESS_KEY
       - AWS_SESSION_TOKEN (optional, for temporary credentials)

    2. **Assume role providers**:
       - AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE (for OIDC/web identity)
       - Configured role assumption in ~/.aws/config

    3. **AWS IAM Identity Center (SSO)**:
       - Configured via `aws sso login` and ~/.aws/config
       - Uses cached SSO token for authentication

    4. **Shared credentials file**:
       - ~/.aws/credentials (credentials stored per profile)

    5. **Shared config file**:
       - ~/.aws/config (can also contain credentials)

    6. **IAM role credentials** (when running in AWS):
       - ECS task role (ECS_CONTAINER_METADATA_URI)
       - EC2 instance profile (EC2 instance metadata service)
       - Lambda execution role

    The first method that provides valid credentials is used.

    ## Profile Selection

    When credentials are stored in files (~/.aws/credentials or ~/.aws/config):

    1. Explicit `profile` parameter (this config)
    2. AWS_PROFILE environment variable
    3. "default" profile from ~/.aws files (if exists)

    ## Region Selection

    1. Explicit `region` parameter (this config)
    2. AWS_REGION or AWS_DEFAULT_REGION environment variable
    3. Region from selected profile in ~/.aws/config
    4. If none specified, AWS operations will fail (no default region)

    ## Examples

    Use environment variables (CI/CD):
    ```python
    # Set in environment: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
    AwsConfig()  # Uses env vars
    ```

    Use SSO (recommended for developers):
    ```bash
    aws sso login --profile my-sso-profile
    ```
    ```python
    AwsConfig(profile="my-sso-profile")
    ```

    Use different profiles per stage:
    ```python
    @app.config
    def config(stage: str) -> StelvioAppConfig:
        if stage == "prod":
            return StelvioAppConfig(aws=AwsConfig(profile="prod-profile"))
        return StelvioAppConfig(aws=AwsConfig())  # Personal/dev uses default
    ```

    Override region:
    ```python
    AwsConfig(region="eu-west-1")  # Deploy to EU regardless of profile region
    ```
    """

    profile: str | None = None
    region: str | None = None


@dataclass(frozen=True, kw_only=True)
class StelvioAppConfig:
    """Stelvio app configuration.

    Attributes:
        aws: AWS credentials and region configuration.
        dns: DNS provider configuration for custom domains.
        environments: List of shared environment names (e.g., ["staging", "production"]).
        home: State storage backend. Currently only "aws" is supported.
    """

    aws: AwsConfig = field(default_factory=AwsConfig)
    dns: Dns | None = None
    environments: list[str] = field(default_factory=list)
    home: Literal["aws"] = "aws"

    def is_valid_environment(self, env: str, username: str) -> bool:
        return env == username or env in self.environments
