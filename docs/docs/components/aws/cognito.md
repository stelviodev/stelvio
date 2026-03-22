# Authentication with Cognito

Stelvio supports [Amazon Cognito](https://aws.amazon.com/cognito/) for user authentication using the `UserPool` component. You can create user pools with email/password sign-in, social login providers, MFA, and Lambda triggers — with Stelvio handling the IAM wiring automatically.

!!! warning "Immutable Settings"
    Some User Pool settings cannot be changed after creation: sign-in identifiers (`usernames`/`aliases`) and required attributes. Plan these carefully before your first deploy.

## Creating a User Pool

The simplest setup is a user pool where users sign in with their email address:

```python
from stelvio.aws.cognito import UserPool
from stelvio.aws.function import Function

@app.run
def run() -> None:
    users = UserPool("users", usernames=["email"])

    web = users.add_client("web",
        callback_urls=["https://app.example.com/callback"],
    )

    api = Function("api",
        handler="functions/api.handler",
        links=[users, web],
    )
```

This creates a Cognito User Pool, an app client for your frontend, and a Lambda function with access to the pool and client IDs.

## Sign-in Options

You must choose one of two sign-in modes when creating a pool:

**Username attributes** — users sign in with email or phone as their username (no separate username field). This is the most common choice:

```python
users = UserPool("users", usernames=["email"])
users = UserPool("users", usernames=["email", "phone"])
```

**Aliases** — users have a traditional username and can also sign in with email, phone, or preferred_username:

```python
users = UserPool("users", aliases=["email", "preferred_username"])
```

!!! info "Choosing Between Usernames and Aliases"
    Most web apps should use `usernames=["email"]`. This is simpler — the user's email IS their username. Use `aliases` only when you need distinct usernames (e.g., gaming, social platforms).

    These modes are **mutually exclusive** and **cannot be changed** after the pool is created.

## App Clients

App clients connect your application to the user pool. You typically create one per platform or trust boundary.

```python
users = UserPool("users", usernames=["email"])

# Public client for a browser SPA (no secret, uses PKCE)
web = users.add_client("web",
    callback_urls=["https://app.example.com/callback"],
    logout_urls=["https://app.example.com/logout"],
)

# Confidential client for your backend (has a secret)
server = users.add_client("server", generate_secret=True)
```

| Parameter | Description |
|-----------|-------------|
| `callback_urls` | Where Cognito redirects after login (required for OAuth flows) |
| `logout_urls` | Where Cognito redirects after logout |
| `providers` | Identity providers this client supports (default: `["COGNITO"]`) |
| `generate_secret` | Create a confidential client with a secret (default: `False`) |

!!! tip "Public vs Confidential Clients"
    Use a **public client** (no secret) for browser SPAs and mobile apps — the client ID is visible in JavaScript. Use a **confidential client** (`generate_secret=True`) for server-side apps where the secret can be kept safe.

## Lambda Triggers

Cognito can invoke Lambda functions at key points in the authentication flow. Stelvio handles creating the functions and granting Cognito invoke permissions automatically.

```python
users = UserPool("users",
    usernames=["email"],
    triggers={
        "pre_sign_up": "functions/auth/validate.handler",
        "post_confirmation": "functions/auth/welcome.handler",
    },
)
```

Each trigger value accepts the same forms as Lambda handlers elsewhere in Stelvio:

- A handler path string: `"functions/auth/validate.handler"`
- A `FunctionConfig` for full control over the Lambda
- An existing `Function` instance to reuse a function

### Common Triggers

| Trigger | When it fires | Common use |
|---------|--------------|------------|
| `pre_sign_up` | Before a user is created | Validate email domain, block disposable emails, auto-confirm |
| `post_confirmation` | After user confirms account | Send welcome email, create database record |
| `pre_authentication` | Before sign-in succeeds | Block users, log attempts |
| `post_authentication` | After sign-in succeeds | Analytics, last-login tracking |
| `pre_token_generation` | Before JWT is issued | Add custom claims to tokens |

### Additional Triggers

| Trigger | Use case |
|---------|----------|
| `user_migration` | Migrate users from an old auth system on first sign-in |
| `custom_message` | Customize verification/welcome email/SMS content |
| `define_auth_challenge` | Custom multi-step authentication flows |
| `create_auth_challenge` | Create challenges for custom auth |
| `verify_auth_challenge_response` | Verify custom auth challenge responses |

### Trigger Example: Validate Email Domain

```python
# functions/auth/validate.py
def handler(event, context):
    email = event["request"]["userAttributes"]["email"]
    domain = email.split("@")[1]

    # Only allow company emails
    if domain != "mycompany.com":
        raise Exception("Only @mycompany.com emails are allowed")

    # Auto-confirm and auto-verify
    event["response"]["autoConfirmUser"] = True
    event["response"]["autoVerifyEmail"] = True

    return event
```

## Social Login (Identity Providers)

Add social or enterprise login providers to your user pool:

```python
google = users.add_identity_provider("google",
    provider_type="google",
    details={
        "authorize_scopes": "email profile",
        "client_id": "your-google-client-id",
        "client_secret": "your-google-client-secret",
    },
    attributes={"email": "email", "username": "sub"},
)

# Enable the provider on a client
web = users.add_client("web",
    callback_urls=["https://app.example.com/callback"],
    providers=[google.provider_name, "COGNITO"],
)
```

### Supported Provider Types

| Type | Provider |
|------|----------|
| `"google"` | Google OAuth |
| `"facebook"` | Facebook Login |
| `"apple"` | Sign in with Apple |
| `"amazon"` | Login with Amazon |
| `"oidc"` | Any OpenID Connect provider |
| `"saml"` | Any SAML 2.0 provider |

### OIDC Provider Example

```python
okta = users.add_identity_provider("okta",
    provider_type="oidc",
    details={
        "client_id": "your-oidc-client-id",
        "client_secret": "your-oidc-client-secret",
        "oidc_issuer": "https://your-tenant.okta.com",
        "authorize_scopes": "openid email profile",
        "attributes_request_method": "GET",
    },
    attributes={"email": "email", "username": "sub"},
)
```

!!! warning "Required OIDC Fields"
    OIDC providers require `client_id`, `authorize_scopes`, `oidc_issuer`, **and** `attributes_request_method` in `details`. Omitting `attributes_request_method` causes a deployment error.

The `details` dictionary varies by provider type. See [AWS documentation](https://docs.aws.amazon.com/cognito/latest/developerguide/cognito-user-pools-identity-federation.html) for provider-specific configuration.

## MFA (Multi-Factor Authentication)

Enable MFA with TOTP (authenticator app):

```python
users = UserPool("users",
    usernames=["email"],
    mfa="optional",       # "off", "optional", or "on"
    software_token=True,  # Enable TOTP (authenticator app)
)
```

| MFA Mode | Behavior |
|----------|----------|
| `"off"` | MFA disabled (default) |
| `"optional"` | Users can enable MFA in their account |
| `"on"` | MFA required for all users |

!!! info "SMS MFA"
    For SMS-based MFA, configure the SMS settings via the `customize` parameter. TOTP (`software_token=True`) is recommended as it doesn't require SMS infrastructure.

## Password Policy

Configure password requirements:

```python
from stelvio.aws.cognito import UserPool, PasswordPolicy

users = UserPool("users",
    usernames=["email"],
    password=PasswordPolicy(
        min_length=12,
        require_symbols=True,
        require_numbers=True,
        require_uppercase=True,
        require_lowercase=True,
    ),
)
```

Default password policy (when not specified) requires 8 characters with uppercase, lowercase, numbers, and symbols.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `min_length` | `8` | Minimum password length |
| `require_lowercase` | `True` | Require at least one lowercase letter |
| `require_uppercase` | `True` | Require at least one uppercase letter |
| `require_numbers` | `True` | Require at least one number |
| `require_symbols` | `True` | Require at least one special character |
| `temporary_password_validity_days` | `7` | Days before admin-created temporary passwords expire |

## Email Delivery with SES

By default, Cognito uses its built-in email service which is limited to **50 emails per day**. For production apps, use Stelvio's `Email` component to send via Amazon SES:

```python
from stelvio.aws.cognito import UserPool
from stelvio.aws.email import Email

email = Email("auth-email", "noreply@myapp.com")

users = UserPool("users",
    usernames=["email"],
    email=email,
)
```

Stelvio automatically configures Cognito to use your SES identity for sending verification codes, password resets, and other emails.

!!! warning "Production Email"
    Without SES configuration, Cognito can only send 50 emails per day. Your 51st user that day won't receive a verification email. Always configure SES for production deployments.

## Feature Tiers

Cognito has three pricing tiers with different features:

```python
users = UserPool("users",
    usernames=["email"],
    tier="essentials",  # "lite", "essentials", or "plus"
)
```

| Tier | Key features | Relative cost |
|------|-------------|---------------|
| `"lite"` | Basic auth, standard MFA, classic hosted UI | Lowest |
| `"essentials"` | Passkeys, email MFA, passwordless, access token customization | Medium |
| `"plus"` | Everything in Essentials + threat protection, compromised password detection | Highest |

The default is `"essentials"`, which matches the AWS default.

## Deletion Protection

Prevent accidental deletion of your user pool:

```python
users = UserPool("users",
    usernames=["email"],
    deletion_protection=True,
)
```

!!! tip "Production Safety"
    Enable `deletion_protection=True` for production user pools. This prevents accidental destruction of your entire user directory during deploys.

## Linking

Using the [linking mechanism](linking.md), you can access Cognito resources in your Lambda functions.

### Linking a User Pool

```python
api = Function("api",
    handler="functions/api.handler",
    links=[users],
)
```

Available properties in your Lambda:

```python
from stlv_resources import Resources

def handler(event, context):
    pool_id = Resources.users.user_pool_id
    pool_arn = Resources.users.user_pool_arn
```

Stelvio automatically grants read permissions (`cognito-idp:GetUser`, `cognito-idp:AdminGetUser`, `cognito-idp:ListUsers`) on the user pool.

### Link Properties

| Component | Property | Description |
|-----------|----------|-------------|
| `UserPool` | `user_pool_id` | The Cognito User Pool ID |
| `UserPool` | `user_pool_arn` | The Cognito User Pool ARN |
| `UserPoolClient` | `client_id` | The app client ID |
| `UserPoolClient` | `user_pool_id` | The parent pool ID |
| `UserPoolClient` | `client_secret` | The client secret (only when `generate_secret=True`) |

!!! note "Default Permissions Are Read-Only"
    The default link grants read-only access (`GetUser`, `AdminGetUser`, `ListUsers`). For user management operations (create/update/delete users, reset passwords), use `StelvioApp.set_user_link_for()` to grant additional permissions.

### Linking a Client

```python
web = users.add_client("web",
    callback_urls=["https://app.example.com/callback"],
)

api = Function("api",
    handler="functions/api.handler",
    links=[web],
)
```

Available properties:

```python
from stlv_resources import Resources

def handler(event, context):
    client_id = Resources.users_web.client_id
    pool_id = Resources.users_web.user_pool_id
    # client_secret is available only when generate_secret=True
```

### Verifying JWTs in Your Lambda

A common pattern is to verify the JWT locally (signature + claims) using Cognito JWKS:

```python
from auth.jwt import verify_cognito_jwt
from stlv_resources import Resources

def handler(event, context):
    token = event["headers"].get("authorization", "").replace("Bearer ", "")

    try:
        claims = verify_cognito_jwt(
            token,
            user_pool_id=Resources.users.user_pool_id,
            client_id=Resources.users_web.client_id,
        )
        return {"statusCode": 200, "body": f"Hello {claims['sub']}"}
    except ValueError:
        return {"statusCode": 401, "body": "Unauthorized"}
```

!!! info "When to Call `cognito-idp:GetUser`"
    `GetUser` is useful when you need live profile attributes from Cognito and you already have a valid **access token**. It is not a replacement for JWT signature verification.

## Customization

The `UserPool` component supports the `customize` parameter to override underlying Pulumi resource properties. For an overview of how customization works, see the [Customization guide](customization.md).

### UserPool Resource Keys

| Resource Key | Pulumi Args Type | Description |
|-------------|-----------------|-------------|
| `user_pool` | [UserPoolArgs](https://www.pulumi.com/registry/packages/aws/api-docs/cognito/userpool/#inputs) | The Cognito User Pool |

### Client Resource Keys (via `add_client(customize=...)`)

| Resource Key | Pulumi Args Type | Description |
|-------------|-----------------|-------------|
| `client` | [UserPoolClientArgs](https://www.pulumi.com/registry/packages/aws/api-docs/cognito/userpoolclient/#inputs) | The User Pool Client |

### Identity Provider Resource Keys (via `add_identity_provider(customize=...)`)

| Resource Key | Pulumi Args Type | Description |
|-------------|-----------------|-------------|
| `identity_provider` | [IdentityProviderArgs](https://www.pulumi.com/registry/packages/aws/api-docs/cognito/identityprovider/#inputs) | The Identity Provider |

### Example: Custom Account Recovery

```python
users = UserPool("users",
    usernames=["email"],
    customize={
        "user_pool": {
            "account_recovery_setting": {
                "recovery_mechanisms": [
                    {"name": "verified_email", "priority": 1},
                    {"name": "verified_phone_number", "priority": 2},
                ],
            },
        },
    },
)
```

### Example: Custom Token Expiration on Client

```python
web = users.add_client("web",
    callback_urls=["https://app.example.com/callback"],
    customize={
        "client": {
            "access_token_validity": 1,   # 1 hour
            "id_token_validity": 1,       # 1 hour
            "refresh_token_validity": 30, # 30 days
            "token_validity_units": {
                "access_token": "hours",
                "id_token": "hours",
                "refresh_token": "days",
            },
        },
    },
)
```

## Next Steps

- [Linking](linking.md) — How linking and environment variables work
- [Lambda Functions](lambda.md) — Function configuration and packaging
- [Customization](customization.md) — Override any Pulumi resource property
