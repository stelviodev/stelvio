# Cognito Component — API Design

This document defines the Stelvio Cognito API. It shows what users write for each scenario, followed by the complete type reference.

This document outlines a proposed Cognito public API for discussion and iteration.

---

## Scenarios

### Scenario 1: Basic email/password auth

The simplest case. A user pool where users sign in with their email address.

```python
from stelvio.aws.cognito import UserPool

users = UserPool("users", usernames=["email"])

web = users.add_client("web",
    callback_urls=["https://app.example.com/callback"],
)
```

This creates:
- A Cognito User Pool with email as the sign-in identifier
- A public app client (no secret) for your frontend

### Scenario 2: Custom password policy

```python
from stelvio.aws.cognito import UserPool, PasswordPolicy

users = UserPool("users",
    usernames=["email"],
    password=PasswordPolicy(min_length=12),
)
```

### Scenario 3: Production email delivery via SES

```python
from stelvio.aws.cognito import UserPool
from stelvio.aws.email import Email

email = Email("auth-email", "noreply@myapp.com")

users = UserPool("users",
    usernames=["email"],
    email=email,
)
```

Stelvio automatically configures Cognito to use SES for email delivery, eliminating the 50 emails/day limit.

### Scenario 4: Lambda triggers

```python
from stelvio.aws.cognito import UserPool

users = UserPool("users",
    usernames=["email"],
    triggers={
        "pre_sign_up": "functions/auth/validate.handler",
        "post_confirmation": "functions/auth/welcome.handler",
    },
)
```

Stelvio automatically:
- Creates Lambda functions for each trigger
- Grants Cognito invoke permissions on each Lambda
- Wires the Lambda ARNs into the User Pool's trigger configuration

Trigger values accept the same forms as Function handlers elsewhere in Stelvio:
- `str` — handler path (e.g., `"functions/auth/validate.handler"`)
- `FunctionConfig` — full function configuration
- `FunctionConfigDict` — dict form of function config
- `Function` — reuse an existing Function instance

### Scenario 5: Social login (Google)

```python
from stelvio.aws.cognito import UserPool

users = UserPool("users", usernames=["email"])

google = users.add_identity_provider("google",
    type="google",
    details={
        "authorize_scopes": "email profile",
        "client_id": "your-google-client-id",
        "client_secret": "your-google-client-secret",
    },
    attributes={"email": "email", "username": "sub"},
)

web = users.add_client("web",
    callback_urls=["https://app.example.com/callback"],
    providers=[google.provider_name, "COGNITO"],
)
```

### Scenario 6: MFA with authenticator app

```python
from stelvio.aws.cognito import UserPool

users = UserPool("users",
    usernames=["email"],
    mfa="optional",
    software_token=True,
)
```

### Scenario 7: Multiple clients (web + server)

```python
web = users.add_client("web",
    callback_urls=["https://app.example.com/callback"],
)

server = users.add_client("server",
    generate_secret=True,
)
```

The web client is public (for SPAs). The server client is confidential (has a secret for backend-to-backend communication).

### Scenario 8: Linking to a Lambda

```python
from stelvio.aws.function import Function

auth_fn = Function("auth-check",
    handler="functions/auth.handler",
    links=[users],
)
```

The Lambda receives:
- `STLV_USERS_USER_POOL_ID` — the pool ID
- `STLV_USERS_USER_POOL_ARN` — the pool ARN
- IAM permissions: `cognito-idp:GetUser`, `cognito-idp:AdminGetUser`, `cognito-idp:ListUsers`

Linking a client:

```python
auth_fn = Function("auth-check",
    handler="functions/auth.handler",
    links=[web],
)
```

The Lambda receives:
- `STLV_USERS_WEB_CLIENT_ID`
- `STLV_USERS_WEB_USER_POOL_ID`
- IAM permissions: same read-only Cognito permissions

### Scenario 9: Full production setup

```python
from stelvio.aws.cognito import UserPool, PasswordPolicy
from stelvio.aws.email import Email
from stelvio.aws.function import Function

email = Email("auth-email", "noreply@myapp.com")

users = UserPool("users",
    usernames=["email"],
    password=PasswordPolicy(min_length=12),
    email=email,
    mfa="optional",
    software_token=True,
    deletion_protection=True,
    triggers={
        "pre_sign_up": "functions/auth/validate.handler",
        "post_confirmation": "functions/auth/welcome.handler",
    },
)

google = users.add_identity_provider("google",
    type="google",
    details={
        "authorize_scopes": "email profile",
        "client_id": "your-google-client-id",
        "client_secret": "your-google-client-secret",
    },
    attributes={"email": "email", "username": "sub"},
)

web = users.add_client("web",
    callback_urls=["https://app.example.com/callback"],
    logout_urls=["https://app.example.com/logout"],
    providers=[google.provider_name, "COGNITO"],
)

server = users.add_client("server", generate_secret=True)

api_fn = Function("api",
    handler="functions/api.handler",
    links=[users, web],
)
```

### Scenario 10: Identity Pool (when frontend needs AWS credentials)

```python
from stelvio.aws.cognito import UserPool, IdentityPool
from stelvio.aws.permission import AwsPermission

users = UserPool("users", usernames=["email"])
web = users.add_client("web", callback_urls=["https://app.example.com/callback"])

identity = IdentityPool("app-identity",
    user_pools=[{"user_pool": users, "client": web}],
    permissions={
        "authenticated": [
            AwsPermission(
                actions=["s3:GetObject", "s3:PutObject"],
                resources=["arn:aws:s3:::my-bucket/private/${cognito-identity.amazonaws.com:sub}/*"],
            ),
        ],
    },
)
```

---

## Type Reference

### UserPool

```python
class UserPool(Component[UserPoolResources, UserPoolCustomizationDict], LinkableMixin):
    def __init__(
        self,
        name: str,
        /,
        *,
        config: UserPoolConfig | UserPoolConfigDict | None = None,
        customize: UserPoolCustomizationDict | None = None,
        **opts: Unpack[UserPoolConfigDict],
    ) -> None: ...
```

#### UserPoolConfig

```python
@dataclass(frozen=True, kw_only=True)
class UserPoolConfig:
    usernames: list[SignInIdentifier] = field(default_factory=list)
    aliases: list[AliasIdentifier] = field(default_factory=list)
    mfa: MfaMode = "off"
    software_token: bool = False
    triggers: TriggerConfigDict | None = None
    password: PasswordPolicy | PasswordPolicyDict | None = None
    email: Email | None = None
    tier: PoolTier = "essentials"
    deletion_protection: bool = False
```

#### Type aliases

```python
SignInIdentifier = Literal["email", "phone"]
AliasIdentifier = Literal["email", "phone", "preferred_username"]
MfaMode = Literal["off", "optional", "on"]
PoolTier = Literal["lite", "essentials", "plus"]
TriggerHandler = str | FunctionConfig | FunctionConfigDict | Function
ProviderName = Input[str]
```

#### TriggerConfigDict

```python
class TriggerConfigDict(TypedDict, total=False):
    # Common triggers
    pre_sign_up: TriggerHandler
    post_confirmation: TriggerHandler
    pre_authentication: TriggerHandler
    post_authentication: TriggerHandler
    pre_token_generation: TriggerHandler

    # Migration
    user_migration: TriggerHandler

    # Custom auth flow
    define_auth_challenge: TriggerHandler
    create_auth_challenge: TriggerHandler
    verify_auth_challenge_response: TriggerHandler

    # Message customization
    custom_message: TriggerHandler
```

`custom_email_sender` and `custom_sms_sender` are out of scope for v1 trigger support.
They require extra KMS and sender configuration and should be set via `customize` for now.

#### PasswordPolicy

```python
@dataclass(frozen=True, kw_only=True)
class PasswordPolicy:
    min_length: int = 8
    require_lowercase: bool = True
    require_uppercase: bool = True
    require_numbers: bool = True
    require_symbols: bool = True
    temporary_password_validity_days: int = 7
```

#### Properties

```python
@property
def arn(self) -> Output[str]: ...

@property
def id(self) -> Output[str]: ...

@property
def name_in_aws(self) -> Output[str]: ...
```

#### UserPoolResources

```python
@dataclass(frozen=True)
class UserPoolResources:
    user_pool: aws.cognito.UserPool
    trigger_functions: dict[str, Function]    # trigger_name -> Function
    trigger_permissions: dict[str, aws.lambda_.Permission]  # trigger_name -> invoke permission
```

#### Customization

```python
class UserPoolCustomizationDict(TypedDict, total=False):
    user_pool: UserPoolArgs | dict[str, Any] | None
```

Trigger functions and permissions are customizable through the Function's own customize parameter (pass `FunctionConfig` with `customize` instead of a handler string).

#### Link behavior

When linked to a Function:

| Property | Env var pattern |
|----------|----------------|
| `user_pool_id` | `STLV_{NAME}_USER_POOL_ID` |
| `user_pool_arn` | `STLV_{NAME}_USER_POOL_ARN` |

Default IAM permissions:
- `cognito-idp:GetUser`
- `cognito-idp:AdminGetUser`
- `cognito-idp:ListUsers`

---

### UserPool.add_client()

```python
def add_client(
    self,
    name: str,
    /,
    *,
    callback_urls: list[str] | None = None,
    logout_urls: list[str] | None = None,
    providers: list[ProviderName] | None = None,
    generate_secret: bool = False,
    customize: UserPoolClientCustomizationDict | None = None,
) -> UserPoolClient: ...
```

Returns a `UserPoolClient` that is `LinkableMixin`.

#### UserPoolClient properties

```python
@property
def client_id(self) -> Output[str]: ...

@property
def client_secret(self) -> Output[str] | None: ...  # None when generate_secret=False
```

#### UserPoolClientResources

```python
@dataclass(frozen=True)
class UserPoolClientResources:
    client: aws.cognito.UserPoolClient
```

#### Client customization

```python
class UserPoolClientCustomizationDict(TypedDict, total=False):
    client: UserPoolClientArgs | dict[str, Any] | None
```

#### Client link behavior

When linked to a Function:

| Property | Env var pattern |
|----------|----------------|
| `client_id` | `STLV_{NAME}_CLIENT_ID` |
| `user_pool_id` | `STLV_{NAME}_USER_POOL_ID` |

When `generate_secret=True`, also includes:

| Property | Env var pattern |
|----------|----------------|
| `client_secret` | `STLV_{NAME}_CLIENT_SECRET` |

Default IAM permissions: same as UserPool (read-only Cognito operations).

---

### UserPool.add_identity_provider()

```python
def add_identity_provider(
    self,
    name: str,
    /,
    *,
    type: IdentityProviderType,
    details: dict[str, str],
    attributes: dict[str, str] | None = None,
    customize: IdentityProviderCustomizationDict | None = None,
) -> IdentityProviderResult: ...
```

```python
IdentityProviderType = Literal["google", "facebook", "apple", "amazon", "oidc", "saml"]
```

#### IdentityProviderResult

Not a full Component. A simple typed object exposing:

```python
@dataclass(frozen=True)
class IdentityProviderResult:
    provider_name: Output[str]  # For use in add_client(providers=[...])
    resources: IdentityProviderResources

@dataclass(frozen=True)
class IdentityProviderResources:
    identity_provider: aws.cognito.IdentityProvider
```

#### Identity provider customization

```python
class IdentityProviderCustomizationDict(TypedDict, total=False):
    identity_provider: IdentityProviderArgs | dict[str, Any] | None
```

---

### IdentityPool

```python
class IdentityPool(Component[IdentityPoolResources, IdentityPoolCustomizationDict], LinkableMixin):
    def __init__(
        self,
        name: str,
        /,
        *,
        config: IdentityPoolConfig | IdentityPoolConfigDict | None = None,
        customize: IdentityPoolCustomizationDict | None = None,
        **opts: Unpack[IdentityPoolConfigDict],
    ) -> None: ...
```

#### IdentityPoolConfig

```python
@dataclass(frozen=True, kw_only=True)
class IdentityPoolConfig:
    user_pools: list[IdentityPoolBinding | IdentityPoolBindingDict]
    permissions: IdentityPoolPermissions | IdentityPoolPermissionsDict | None = None
    allow_unauthenticated: bool = False
```

```python
@dataclass(frozen=True, kw_only=True)
class IdentityPoolBinding:
    user_pool: UserPool | str     # UserPool component or pool ID string
    client: UserPoolClient | str  # UserPoolClient component or client ID string

@dataclass(frozen=True, kw_only=True)
class IdentityPoolPermissions:
    authenticated: list[AwsPermission] = field(default_factory=list)
    unauthenticated: list[AwsPermission] = field(default_factory=list)
```

#### IdentityPoolResources

```python
@dataclass(frozen=True)
class IdentityPoolResources:
    identity_pool: aws.cognito.IdentityPool
    authenticated_role: aws.iam.Role
    unauthenticated_role: aws.iam.Role | None
    roles_attachment: aws.cognito.IdentityPoolRolesAttachment
```

#### Properties

```python
@property
def id(self) -> Output[str]: ...

@property
def authenticated_role_arn(self) -> Output[str]: ...

@property
def unauthenticated_role_arn(self) -> Output[str] | None: ...
```

#### Identity pool customization

```python
class IdentityPoolCustomizationDict(TypedDict, total=False):
    identity_pool: IdentityPoolArgs | dict[str, Any] | None
    authenticated_role: RoleArgs | dict[str, Any] | None
    unauthenticated_role: RoleArgs | dict[str, Any] | None
    roles_attachment: IdentityPoolRolesAttachmentArgs | dict[str, Any] | None
```

#### Identity pool link behavior

When linked to a Function:

| Property | Env var pattern |
|----------|----------------|
| `identity_pool_id` | `STLV_{NAME}_IDENTITY_POOL_ID` |

Default IAM permissions: none (Identity Pool is an authorization layer, not something Lambdas typically call).

---

## Validation Rules

All fail fast with `ValueError`:

| Rule | Error message |
|------|---------------|
| `config` + `**opts` both provided | `"cannot combine 'config' with additional options"` |
| `usernames` + `aliases` both set | `"usernames and aliases are mutually exclusive"` |
| `mfa` is `"on"` or `"optional"` without `software_token=True` and no SMS | `"mfa requires software_token=True or SMS configuration via customize"` |
| Unknown trigger key provided | `"unknown trigger '<name>' (allowed: ...)"` |
| Duplicate client name | `"Client 'web' already exists for user pool 'users'"` |
| Duplicate identity provider name | `"Identity provider 'google' already exists for user pool 'users'"` |
| Modify after resources created | `RuntimeError("Cannot modify UserPool 'users' after resources have been created")` |
| IdentityPool with empty `user_pools` | `"user_pools must contain at least one binding"` |
| IdentityPool unauth permissions without `allow_unauthenticated=True` | `"unauthenticated permissions require allow_unauthenticated=True"` |
