# Cognito Component — Implementation Guide

This document provides implementation details for building the Cognito components following Stelvio patterns. Reference the [API design](api-design.md) for the public interface.

## File Layout (Proposed)

```
stelvio/aws/cognito/
    __init__.py          # Public API exports
    user_pool.py         # UserPool component + UserPoolClient + IdentityProviderResult
    identity_pool.py     # IdentityPool component
    types.py             # Shared config dataclasses, TypedDicts, type aliases
```

This is a suggested layout, not a strict requirement.
Bas can choose either:

- A compact layout (as shown above), or
- A more split layout if it improves maintainability (for example separate `client.py` / `identity_provider.py` modules).

Both are valid as long as the public API and Stelvio component patterns stay consistent.

### Public exports (`__init__.py`)

```python
from stelvio.aws.cognito.types import (
    UserPoolConfig,
    UserPoolConfigDict,
    PasswordPolicy,
    PasswordPolicyDict,
    TriggerConfigDict,
    IdentityPoolConfig,
    IdentityPoolConfigDict,
    IdentityPoolBinding,
    IdentityPoolBindingDict,
    IdentityPoolPermissions,
    IdentityPoolPermissionsDict,
)
from stelvio.aws.cognito.user_pool import UserPool, UserPoolClient, IdentityProviderResult
from stelvio.aws.cognito.identity_pool import IdentityPool
```

---

## UserPool Component

### Class structure

Follow `DynamoTable` as the primary pattern:

```python
@final
class UserPool(Component[UserPoolResources, UserPoolCustomizationDict], LinkableMixin):
    _config: UserPoolConfig
    _clients: list[UserPoolClient]
    _identity_providers: list[IdentityProviderResult]

    def __init__(
        self,
        name: str,
        /,
        *,
        config: UserPoolConfig | UserPoolConfigDict | None = None,
        customize: UserPoolCustomizationDict | None = None,
        **opts: Unpack[UserPoolConfigDict],
    ) -> None:
        super().__init__("stelvio:aws:UserPool", name, customize=customize)
        self._config = self._parse_config(config, opts)
        self._clients = []
        self._identity_providers = []
```

**Key patterns:**
- Type URN: `"stelvio:aws:UserPool"` (consistent with all 19 existing components)
- `_parse_config()` static method: same pattern as Queue, DynamoTable, Function
- Builder lists (`_clients`, `_identity_providers`): same pattern as Api (`_routes`, `_authorizers`)

### Config parsing

Same as Queue/DynamoTable:

```python
@staticmethod
def _parse_config(
    config: UserPoolConfig | UserPoolConfigDict | None,
    opts: UserPoolConfigDict,
) -> UserPoolConfig:
    if config and opts:
        raise ValueError(
            "Invalid configuration: cannot combine 'config' parameter with additional options "
            "- provide all settings either in 'config' or as separate options"
        )
    if config is None:
        return UserPoolConfig(**opts)
    if isinstance(config, UserPoolConfig):
        return config
    if isinstance(config, dict):
        return UserPoolConfig(**config)
    raise TypeError(...)
```

### Validation

In `UserPoolConfig.__post_init__()`:

```python
def __post_init__(self):
    if self.usernames and self.aliases:
        raise ValueError(
            "usernames and aliases are mutually exclusive - "
            "use usernames to let users sign in with email/phone as their username, "
            "or aliases to let users sign in with username plus email/phone/preferred_username as aliases"
        )
    if self.mfa in ("on", "optional") and not self.software_token:
        # Check if SMS is configured via customize (we can't easily check this)
        # For now, require software_token. SMS can be set via customize.
        raise ValueError(
            "mfa='on' or 'optional' requires software_token=True. "
            "For SMS-based MFA, configure SMS settings via the customize parameter."
        )
```

### Builder methods

#### add_client()

Follow the Api `add_*_authorizer()` pattern with `_check_not_created()` guard:

```python
def add_client(
    self,
    name: str,
    /,
    *,
    callback_urls: list[str] | None = None,
    logout_urls: list[str] | None = None,
    providers: list[Input[str]] | None = None,
    generate_secret: bool = False,
    customize: UserPoolClientCustomizationDict | None = None,
) -> "UserPoolClient":
    self._check_not_created()

    # Check duplicate names
    if any(c._client_name == name for c in self._clients):
        raise ValueError(f"Client '{name}' already exists for user pool '{self.name}'")

    client = UserPoolClient(
        pool=self,
        client_name=name,
        callback_urls=callback_urls,
        logout_urls=logout_urls,
        providers=providers,
        generate_secret=generate_secret,
        customize=customize,
    )
    self._clients.append(client)
    return client
```

#### add_identity_provider()

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
) -> IdentityProviderResult:
    self._check_not_created()

    if any(p._provider_name == name for p in self._identity_providers):
        raise ValueError(
            f"Identity provider '{name}' already exists for user pool '{self.name}'"
        )

    result = IdentityProviderResult(
        pool=self,
        provider_name_str=name,
        type=type,
        details=details,
        attributes=attributes,
        customize=customize,
    )
    self._identity_providers.append(result)
    return result
```

### `_create_resources()`

This is the main implementation method. Order matters for dependencies:

```python
def _create_resources(self) -> UserPoolResources:
    # 1. Build password policy args
    password_args = self._build_password_policy()

    # 2. Build email configuration args (if Email component provided)
    email_args = self._build_email_config()

    # 3. Create trigger functions first so their ARNs can be included in pool lambda_config
    trigger_functions: dict[str, Function] = {}
    lambda_config_args: dict[str, Input[str]] = {}
    if self._config.triggers:
        for trigger_name, handler in self._config.triggers.items():
            fn = self._create_trigger_function(trigger_name, handler)
            trigger_functions[trigger_name] = fn
            config_key = self._trigger_name_to_config_key(trigger_name)
            lambda_config_args[config_key] = fn.resources.function.arn

    # 4. Create the User Pool resource with lambda_config
    pool = aws.cognito.UserPool(
        safe_name(context().prefix(), self.name, 128),
        **self._customizer("user_pool", {
            "name": safe_name(context().prefix(), self.name, 128),
            "username_attributes": self._config.usernames or None,
            "alias_attributes": self._config.aliases or None,
            "auto_verified_attributes": self._auto_verified_attributes(),
            "mfa_configuration": self._mfa_mode_to_aws(),
            "software_token_mfa_configuration": (
                {"enabled": True} if self._config.software_token else None
            ),
            "password_policy": password_args,
            "email_configuration": email_args,
            "deletion_protection": "ACTIVE" if self._config.deletion_protection else "INACTIVE",
            "user_pool_tier": self._config.tier.upper(),
            "lambda_config": lambda_config_args or None,
        }),
        opts=self._resource_opts(),
    )

    # 5. Create Lambda invoke permissions for Cognito (need both pool ARN and function)
    trigger_permissions: dict[str, aws.lambda_.Permission] = {}
    for trigger_name, fn in trigger_functions.items():
        trigger_permissions[trigger_name] = self._create_trigger_permission(
            trigger_name, fn, pool
        )

    # 6. Create identity providers (depend on pool)
    for provider_result in self._identity_providers:
        provider_result._create_resource(pool)

    # 7. Create clients (depend on pool and possibly providers)
    for client in self._clients:
        depends = [p._resource for p in self._identity_providers]
        client._create_resource(pool, depends_on=depends)

    # 8. Register outputs
    self.register_outputs({
        "user_pool_id": pool.id,
        "arn": pool.arn,
    })

    # 9. Export
    pulumi.export(f"cognito_{self.name}_id", pool.id)
    pulumi.export(f"cognito_{self.name}_arn", pool.arn)

    return UserPoolResources(
        user_pool=pool,
        trigger_functions=trigger_functions,
        trigger_permissions=trigger_permissions,
    )
```

### Trigger creation

Follow the pattern from Api's authorizer function creation:

```python
def _create_trigger_function(
    self,
    trigger_name: str,
    handler: TriggerHandler,
) -> Function:
    """Create or reuse a trigger Function."""
    if isinstance(handler, Function):
        return handler
    if isinstance(handler, str):
        return Function(
            f"{self.name}-trigger-{trigger_name}",
            handler=handler,
        )
    if isinstance(handler, FunctionConfig):
        return Function(
            f"{self.name}-trigger-{trigger_name}",
            config=handler,
        )
    return Function(
        f"{self.name}-trigger-{trigger_name}",
        config=handler,
    )
```

```python
def _create_trigger_permission(
    self,
    trigger_name: str,
    fn: Function,
    pool: aws.cognito.UserPool,
) -> aws.lambda_.Permission:
    """Allow Cognito to invoke a trigger function."""
    return aws.lambda_.Permission(
        safe_name(context().prefix(), f"{self.name}-trigger-{trigger_name}-perm", 128),
        action="lambda:InvokeFunction",
        function=fn.function_name,
        principal="cognito-idp.amazonaws.com",
        source_arn=pool.arn,
        opts=self._resource_opts(depends_on=[fn.resources.function]),
    )
```

### Trigger name mapping

Map Stelvio trigger names to Pulumi `UserPoolLambdaConfig` keys:

```python
TRIGGER_CONFIG_MAP = {
    "pre_sign_up": "pre_sign_up",
    "post_confirmation": "post_confirmation",
    "pre_authentication": "pre_authentication",
    "post_authentication": "post_authentication",
    "pre_token_generation": "pre_token_generation",
    "user_migration": "user_migration",
    "define_auth_challenge": "define_auth_challenge",
    "create_auth_challenge": "create_auth_challenge",
    "verify_auth_challenge_response": "verify_auth_challenge_response",
    "custom_message": "custom_message",
}
```
`custom_email_sender` and `custom_sms_sender` are explicitly out of scope for v1 and must be configured through `customize` until dedicated support is added.

### Email configuration

```python
def _build_email_config(self) -> dict | None:
    if self._config.email is None:
        return None

    email_component = self._config.email
    identity = email_component.resources.identity

    return {
        "email_sending_account": "DEVELOPER",
        "source_arn": identity.arn,
        "from_email_address": identity.email_identity,
    }
```

### Auto-verified attributes

When `usernames` includes `"email"`, auto-verify email. When it includes `"phone"`, auto-verify phone.

```python
def _auto_verified_attributes(self) -> list[str] | None:
    attrs = []
    identifiers = self._config.usernames or self._config.aliases or []
    if "email" in identifiers:
        attrs.append("email")
    if "phone" in identifiers:
        attrs.append("phone_number")
    return attrs or None
```

### MFA mode mapping

```python
def _mfa_mode_to_aws(self) -> str:
    return {
        "off": "OFF",
        "optional": "OPTIONAL",
        "on": "ON",
    }[self._config.mfa]
```

---

## UserPoolClient

### Design choice: Component or not?

`UserPoolClient` should be a **Component** with `LinkableMixin` because:
- It needs to be linkable (Functions need client IDs)
- It has its own resources
- It registers in ComponentRegistry

Component name: `"{pool_name}-{client_name}"` (e.g., `"users-web"`).

```python
@final
class UserPoolClient(Component[UserPoolClientResources, UserPoolClientCustomizationDict], LinkableMixin):
    def __init__(
        self,
        pool: UserPool,
        client_name: str,
        callback_urls: list[str] | None,
        logout_urls: list[str] | None,
        providers: list[Input[str]] | None,
        generate_secret: bool,
        customize: UserPoolClientCustomizationDict | None,
    ):
        # Component name combines pool + client for global uniqueness
        super().__init__(
            "stelvio:aws:UserPoolClient",
            f"{pool.name}-{client_name}",
            customize=customize,
        )
        self._pool = pool
        self._client_name = client_name
        self._callback_urls = callback_urls
        self._logout_urls = logout_urls
        self._providers = providers
        self._generate_secret = generate_secret

    def _create_resource(
        self,
        pool: aws.cognito.UserPool,
        depends_on: list | None = None,
    ) -> None:
        """Called by UserPool._create_resources(). Not the standard _create_resources() pattern
        because we need the pool resource reference."""

        has_oauth = bool(self._callback_urls or self._logout_urls)

        client = aws.cognito.UserPoolClient(
            safe_name(context().prefix(), f"{self._pool.name}-{self._client_name}", 128),
            **self._customizer("client", {
                "name": safe_name(context().prefix(), f"{self._pool.name}-{self._client_name}", 128),
                "user_pool_id": pool.id,
                "generate_secret": self._generate_secret,
                "callback_urls": self._callback_urls,
                "logout_urls": self._logout_urls,
                "supported_identity_providers": self._providers or ["COGNITO"],
                "allowed_oauth_flows_user_pool_client": has_oauth,
                "allowed_oauth_flows": ["code"] if has_oauth else None,
                "allowed_oauth_scopes": (
                    ["openid", "email", "profile"] if has_oauth else None
                ),
            }),
            opts=self._resource_opts(depends_on=depends_on),
        )

        self._resources = UserPoolClientResources(client=client)

        self.register_outputs({
            "client_id": client.id,
        })
        pulumi.export(f"cognito_{self._pool.name}_{self._client_name}_client_id", client.id)

    def _create_resources(self) -> UserPoolClientResources:
        # Resources are created by UserPool._create_resources() via _create_resource()
        # This should not be called directly via lazy .resources access
        raise RuntimeError(
            f"UserPoolClient '{self.name}' resources are created by its parent UserPool. "
            "Access the parent UserPool's .resources first."
        )
```

**Important pattern note**: Unlike standalone components where `_create_resources()` is called lazily via `.resources`, UserPoolClient's resources are created by the parent UserPool. This is intentional because client creation requires the parent pool resource reference.

### Client link creator

```python
@link_config_creator(UserPoolClient)
def default_user_pool_client_link(client: UserPoolClient) -> LinkConfig:
    client_resource = client.resources.client
    pool = client._pool

    properties = {
        "client_id": client_resource.id,
        "user_pool_id": pool.id,
    }

    if client._generate_secret:
        properties["client_secret"] = client_resource.client_secret

    return LinkConfig(
        properties=properties,
        permissions=[
            AwsPermission(
                actions=[
                    "cognito-idp:GetUser",
                    "cognito-idp:AdminGetUser",
                    "cognito-idp:ListUsers",
                ],
                resources=[pool.arn],
            ),
        ],
    )
```

---

## IdentityProviderResult

Not a full Component. A lightweight wrapper created by `add_identity_provider()`:

```python
@dataclass
class IdentityProviderResult:
    _pool: UserPool
    _provider_name: str
    _type: IdentityProviderType
    _details: dict[str, str]
    _attributes: dict[str, str] | None
    _customize: IdentityProviderCustomizationDict | None
    _resource: aws.cognito.IdentityProvider | None = field(default=None, init=False)

    @property
    def provider_name(self) -> Output[str]:
        """The provider name for use in add_client(providers=[...])."""
        if self._resource:
            return self._resource.provider_name
        return Output.from_input(self._provider_name)

    def _create_resource(self, pool: aws.cognito.UserPool) -> None:
        """Called by UserPool._create_resources()."""
        provider_type_map = {
            "google": "Google",
            "facebook": "Facebook",
            "apple": "SignInWithApple",
            "amazon": "LoginWithAmazon",
            "oidc": "OIDC",
            "saml": "SAML",
        }
        provider_name = (
            self._provider_name
            if self._type in ("oidc", "saml")
            else provider_type_map[self._type]
        )
        provider = aws.cognito.IdentityProvider(
            safe_name(context().prefix(), f"{self._pool.name}-idp-{self._provider_name}", 128),
            user_pool_id=pool.id,
            provider_name=provider_name,
            provider_type=provider_type_map[self._type],
            provider_details=self._details,
            attribute_mapping=self._attributes,
            opts=self._pool._resource_opts(),
        )
        self._resource = provider
```

**Why not a Component?** Identity providers are never linked to Functions. They don't need to be in ComponentRegistry. They're only referenced by `provider_name` when calling `add_client(providers=[...])`. A lightweight wrapper is sufficient.

---

## IdentityPool Component

Follows the standard Component pattern. Simpler than UserPool because it has no builder methods.

```python
@final
class IdentityPool(Component[IdentityPoolResources, IdentityPoolCustomizationDict], LinkableMixin):
    _config: IdentityPoolConfig

    def __init__(
        self,
        name: str,
        /,
        *,
        config: IdentityPoolConfig | IdentityPoolConfigDict | None = None,
        customize: IdentityPoolCustomizationDict | None = None,
        **opts: Unpack[IdentityPoolConfigDict],
    ) -> None:
        super().__init__("stelvio:aws:IdentityPool", name, customize=customize)
        self._config = self._parse_config(config, opts)

    def _create_resources(self) -> IdentityPoolResources:
        # 1. Resolve user pool bindings to provider strings
        cognito_providers = self._build_cognito_providers()

        # 2. Create Identity Pool
        identity_pool = aws.cognito.IdentityPool(
            safe_name(context().prefix(), self.name, 128),
            **self._customizer("identity_pool", {
                "identity_pool_name": safe_name(context().prefix(), self.name, 128),
                "allow_unauthenticated_identities": self._config.allow_unauthenticated,
                "cognito_identity_providers": cognito_providers,
            }),
            opts=self._resource_opts(),
        )

        # 3. Create authenticated IAM role
        auth_role = self._create_role(
            "authenticated",
            identity_pool,
            self._config.permissions.authenticated if self._config.permissions else [],
        )

        # 4. Create unauthenticated IAM role (if needed)
        unauth_role = None
        if self._config.allow_unauthenticated:
            unauth_role = self._create_role(
                "unauthenticated",
                identity_pool,
                self._config.permissions.unauthenticated if self._config.permissions else [],
            )

        # 5. Attach roles to identity pool
        roles = {"authenticated": auth_role.arn}
        if unauth_role:
            roles["unauthenticated"] = unauth_role.arn

        roles_attachment = aws.cognito.IdentityPoolRolesAttachment(
            safe_name(context().prefix(), f"{self.name}-roles", 128),
            **self._customizer("roles_attachment", {
                "identity_pool_id": identity_pool.id,
                "roles": roles,
            }),
            opts=self._resource_opts(),
        )

        self.register_outputs({"identity_pool_id": identity_pool.id})
        pulumi.export(f"identity_{self.name}_id", identity_pool.id)

        return IdentityPoolResources(
            identity_pool=identity_pool,
            authenticated_role=auth_role,
            unauthenticated_role=unauth_role,
            roles_attachment=roles_attachment,
        )
```

### Role creation with Cognito trust policy

```python
def _create_role(
    self,
    role_type: str,  # "authenticated" or "unauthenticated"
    identity_pool: aws.cognito.IdentityPool,
    permissions: list[AwsPermission],
) -> aws.iam.Role:
    """Create an IAM role with Cognito Identity trust policy."""

    amr_value = "authenticated" if role_type == "authenticated" else "unauthenticated"

    assume_role_policy = identity_pool.id.apply(lambda pool_id: json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Federated": "cognito-identity.amazonaws.com"},
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    "cognito-identity.amazonaws.com:aud": pool_id,
                },
                "ForAnyValue:StringLike": {
                    "cognito-identity.amazonaws.com:amr": amr_value,
                },
            },
        }],
    }))

    role = aws.iam.Role(
        safe_name(context().prefix(), f"{self.name}-{role_type}-role", 128),
        **self._customizer(f"{role_type}_role", {
            "assume_role_policy": assume_role_policy,
        }),
        opts=self._resource_opts(),
    )

    # Attach inline policy if permissions specified
    if permissions:
        resolved_resource_lists = [
            pulumi.Output.all(
                *(perm.resources if isinstance(perm.resources, list) else [perm.resources])
            ).apply(lambda values: list(values))
            for perm in permissions
        ]
        aws.iam.RolePolicy(
            safe_name(context().prefix(), f"{self.name}-{role_type}-policy", 128),
            role=role.id,
            policy=pulumi.Output.all(*resolved_resource_lists).apply(
                lambda resource_lists: json.dumps(
                    {
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": perm.actions,
                                "Resource": resource_lists[idx],
                            }
                            for idx, perm in enumerate(permissions)
                        ],
                    }
                )
            ),
            opts=self._resource_opts(),
        )

    return role
```

---

## Testing Strategy

### Unit tests

Follow `tests/aws/test_dynamo_db.py` patterns with Pulumi mocks:

```
tests/aws/cognito/
    test_user_pool.py          # UserPool creation, config validation, triggers
    test_user_pool_client.py   # Client creation, generate_secret, OAuth config
    test_identity_provider.py  # Provider wiring
    test_identity_pool.py      # IdentityPool creation, roles, bindings
    test_links.py              # Link properties and permissions
```

Key test cases:

1. **Basic creation**: UserPool with just `usernames=["email"]`
2. **Config/opts duality**: Test both `config=` and `**opts` patterns
3. **Validation**: Test all ValueError cases (mutual exclusion, duplicate names, MFA without software_token)
4. **Triggers**: Test that Functions and Permissions are created for each trigger
5. **add_client**: Test client creation, `_check_not_created()` guard, duplicate detection
6. **Email integration**: Test that Email component wires `email_configuration` correctly
7. **Links**: Test env var names and IAM permissions for both UserPool and UserPoolClient
8. **IdentityPool**: Test role creation, trust policies, permission attachment

### Integration tests

```
tests/integration/test_cognito.py
```

Key integration tests:

1. Basic UserPool + client deploy/destroy lifecycle
2. UserPool with trigger Lambda (verify Lambda is invocable)
3. UserPool + Email component (verify SES email config)
4. UserPool + client + identity provider (verify OAuth flow setup)

---

## Implementation Order

### Phase 1 (ship first)

1. `types.py` — All config dataclasses, TypedDicts, type aliases
2. `UserPool` component — Constructor, config parsing, validation, `_create_resources()`
3. Trigger creation — Function + Permission wiring
4. `UserPoolClient` — `add_client()`, client creation, OAuth defaults
5. `IdentityProviderResult` — `add_identity_provider()`
6. Link creators — For both UserPool and UserPoolClient
7. Email integration — Wire Email component to Cognito email_configuration
8. Unit tests
9. Integration tests (basic lifecycle)

### Phase 2 (follow-up)

1. `IdentityPool` component
2. Domain configuration (hosted UI)
3. Additional integration tests
