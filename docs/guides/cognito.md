# Cognito

Stelvio supports Amazon Cognito User Pools through the `UserPool` component.

Phase 1 includes:

- User pools
- App clients
- Lambda triggers
- Social/OIDC/SAML identity providers
- Linking for pool and client resources
- SES-backed email sending through `Email`

`IdentityPool` support is planned for a later phase.

## Creating A User Pool

```python
from stelvio import app
from stelvio.aws.cognito import UserPool

@app.run
def run() -> None:
    users = UserPool("users", usernames=["email"])
```

`UserPool("users")` also works with defaults.

## Sign-In Options

Use either `usernames` or `aliases`.

```python
# Username attributes (email/phone are treated as usernames)
UserPool("users", usernames=["email"])

# Alias attributes (users sign in with username, aliases are alternate identifiers)
UserPool("users-with-alias", aliases=["email", "preferred_username"])
```

`usernames` and `aliases` are mutually exclusive.

## App Clients

Create app clients with `add_client()` before accessing `pool.resources`.

```python
pool = UserPool("users", usernames=["email"])

# Public client (no secret)
public_client = pool.add_client(
    "web",
    callback_urls=["https://app.example.com/callback"],
    logout_urls=["https://app.example.com/logout"],
)

# Confidential client (client secret enabled)
confidential_client = pool.add_client("backend", generate_secret=True)
```

When callback/logout URLs are provided, Stelvio enables OAuth code flow with standard OpenID scopes.

## Lambda Triggers

Configure Cognito triggers with handler strings, `FunctionConfig`, dict config, or existing `Function` objects.

```python
from stelvio.aws.cognito import UserPool
from stelvio.aws.function import Function, FunctionConfig

existing = Function("shared-auth-fn", handler="functions/auth/shared.handler")

pool = UserPool(
    "users",
    usernames=["email"],
    triggers={
        "pre_sign_up": "functions/auth/validate.handler",
        "post_confirmation": FunctionConfig(handler="functions/auth/welcome.handler"),
        "custom_message": existing,
    },
)
```

Supported trigger keys:

- `pre_sign_up`
- `post_confirmation`
- `pre_authentication`
- `post_authentication`
- `pre_token_generation`
- `user_migration`
- `define_auth_challenge`
- `create_auth_challenge`
- `verify_auth_challenge_response`
- `custom_message`

## Social Login

Add identity providers with `add_identity_provider()` and wire them into clients with `providers=[...]`.

```python
pool = UserPool("users", usernames=["email"])

google = pool.add_identity_provider(
    "google",
    type="google",
    details={
        "client_id": "<google-client-id>",
        "client_secret": "<google-client-secret>",
        "authorize_scopes": "openid email profile",
    },
)

pool.add_client(
    "web",
    callback_urls=["https://app.example.com/callback"],
    providers=[google.provider_name, "COGNITO"],
)
```

For social providers (`google`, `facebook`, `apple`, `amazon`), Cognito provider names are mapped automatically.
For `oidc` and `saml`, Stelvio uses the provider name you pass in.

## MFA

`mfa` accepts `"off"`, `"optional"`, or `"on"`.

```python
UserPool(
    "users",
    usernames=["email"],
    mfa="optional",
    software_token=True,
)
```

When `mfa` is `"on"` or `"optional"`, `software_token=True` is required.

SMS MFA can be configured via `customize={"user_pool": {...}}`.

## Password Policy

Use the `PasswordPolicy` dataclass or a dictionary.

```python
from stelvio.aws.cognito import PasswordPolicy, UserPool

UserPool(
    "users",
    usernames=["email"],
    password=PasswordPolicy(min_length=12, require_symbols=False),
)
```

If omitted, Cognito defaults are used through Stelvio:

- Minimum length: `8`
- Lowercase/uppercase/numbers/symbols: required
- Temporary password validity: `7` days

## Email Delivery With SES

Use Stelvio `Email` with Cognito to set Cognito email sending mode to `DEVELOPER` and attach the SES identity ARN.

```python
from stelvio.aws.cognito import UserPool
from stelvio.aws.email import Email

sender = Email("auth-sender", "noreply@example.com")

pool = UserPool(
    "users",
    usernames=["email"],
    email=sender,
)
```

## Feature Tiers

Set Cognito feature tier with `tier`.

```python
UserPool("users-lite", usernames=["email"], tier="lite")
UserPool("users-plus", usernames=["email"], tier="plus")
```

Allowed values are `"lite"`, `"essentials"`, and `"plus"`.

## Deletion Protection

```python
UserPool("prod-users", usernames=["email"], deletion_protection=True)
```

When enabled, the underlying pool is created with deletion protection active.

## Linking

`UserPool` and `UserPoolClient` are linkable.

- Linking a `UserPool` exposes:
  - `user_pool_id`
  - `user_pool_arn`
- Linking a `UserPoolClient` exposes:
  - `client_id`
  - `user_pool_id`
  - `client_secret` (only when `generate_secret=True`)

Both links grant read-only user lookup permissions (`GetUser`, `AdminGetUser`, `ListUsers`) on the user pool.

Example JWT verification pattern:

```python
from stlv_resources import Resources

pool_id = Resources.users.user_pool_id
region = "us-east-1"
jwks_url = f"https://cognito-idp.{region}.amazonaws.com/{pool_id}/.well-known/jwks.json"
```

## Customization

The Cognito component supports Pulumi resource overrides via `customize`.

### Resource Keys

| Resource Key | Pulumi Args Type |
|---|---|
| `user_pool` | `UserPoolArgs` |
| `client` | `UserPoolClientArgs` |
| `identity_provider` | `IdentityProviderArgs` |

### Examples

```python
pool = UserPool(
    "users",
    usernames=["email"],
    customize={
        "user_pool": {
            "account_recovery_setting": {
                "recovery_mechanisms": [
                    {"name": "verified_email", "priority": 1}
                ]
            }
        }
    },
)

pool.add_client(
    "web",
    customize={"client": {"prevent_user_existence_errors": "ENABLED"}},
)

pool.add_identity_provider(
    "google",
    type="google",
    details={
        "client_id": "<id>",
        "client_secret": "<secret>",
        "authorize_scopes": "openid email profile",
    },
    customize={"identity_provider": {"idp_identifiers": ["accounts.google.com"]}},
)
```
