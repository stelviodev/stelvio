# Working with HTTP APIs in Stelvio

This guide explains how to build HTTP APIs with Stelvio using
[Amazon API Gateway HTTP APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api.html).
You'll learn how to define routes, protect them with authorizers, configure CORS,
and put your API behind a custom domain.

HTTP APIs are the second generation of API Gateway. They're cheaper and faster than
REST APIs, use the Lambda payload format 2.0, and come with native CORS support and
auto-deployed stages. Use `HttpApi` for new Lambda-backed HTTP endpoints unless you
need behavior from API Gateway REST APIs, such as edge-optimized endpoints, token
or request authorizers, or when your existing handlers or libraries expect the
v1 event format. For those cases, reach for [`RestApi`](rest-api.md).

## Creating an HTTP API

Creating an HTTP API in Stelvio is straightforward:

```python
from stelvio.aws.api_gateway import HttpApi

api = HttpApi("users-api")
```

The name you provide is used for naming the underlying AWS resources and for
identifying the API in the AWS console.

Once you have an API instance, you add routes to it:

```python
from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.dynamo_db import DynamoTable

users = DynamoTable("users", fields={"id": "string"}, partition_key="id")

api = HttpApi("users-api", cors=True)

api.route("GET", "/users", "functions/users.list")
api.route("POST", "/users", "functions/users.create", links=[users])
api.route(["GET", "DELETE"], "/users/{id}", "functions/users.detail")
```

That's enough for a working API. Stelvio creates the API Gateway HTTP API, a stage,
a Lambda function and integration for each handler, the necessary IAM permissions,
and a CloudWatch log group for access logs.

### API Configuration

For production use cases, you can configure your HTTP API with additional settings:

```python
from stelvio.aws.api_gateway import ApiDomain, HttpApi

# Basic API with default settings
api = HttpApi("users-api")

# API with permissive CORS defaults
api = HttpApi("users-api", cors=True)

# API with a custom domain owned by this API
api = HttpApi("users-api", domain_name="api.example.com")

# API with a shared custom domain
domain = ApiDomain("public-domain", domain_name="api.example.com")
api = HttpApi("users-api", domain=domain, api_mapping_key="users")

# API with a named stage instead of the default $default stage
api = HttpApi("users-api", stage_name="production")
```

Available configuration options:

| Option | Default | Description |
|--------|---------|-------------|
| `domain_name` | `None` | Custom domain name for an API-owned domain. Requires a DNS provider. Cannot be combined with `domain`. |
| `domain` | `None` | Shared `ApiDomain` component for mapping multiple HTTP APIs to one domain. Cannot be combined with `domain_name`. |
| `api_mapping_key` | `None` | Path segment for a custom domain mapping, such as `admin` or `partners/v1`. Requires `domain_name` or `domain`. |
| `stage_name` | `"$default"` | HTTP API stage name. Use `"$default"` or a name containing letters, numbers, hyphens, and underscores. The `$default` stage serves at the root of the API URL; a named stage adds a path segment, such as `/production`. |
| `cors` | `None` | CORS configuration. Use `True` for permissive defaults or `CorsConfig` for explicit settings. |
| `disable_execute_api_endpoint` | `False` | Disable the default `execute-api` hostname. Requires a custom domain. |
| `access_log_retention_days` | `30` | One of CloudWatch's retention values (`1`, `3`, `5`, `7`, `14`, `30`, `60`, `90`, …). Set to `"forever"` to keep logs indefinitely. |

!!! note "One domain option at a time"
    Set either `domain_name` or `domain`, not both. Combining them raises an error.

!!! warning "Add routes before resource creation"
    Add all routes and authorizers before accessing properties that create resources,
    such as `api.resources`, `api.arn`, `api.api_id`, or `api.execution_arn`.
    After resources are created, Stelvio rejects further route and authorizer changes.

    `api.url` also creates resources when the API uses the default `execute-api`
    hostname. When `domain_name` or `domain` is configured, `api.url` can be
    computed from the custom domain and does not create resources by itself.

## Defining Routes

The basic pattern for routes is the same as the REST API component:

```python
api.route(http_method, path, handler)
```

You can use any standard HTTP method, a list of methods, or `"ANY"` (alternatively
`"*"`) to match all methods:

```python
api.route("GET", "/users", "functions/users.list")
api.route(["POST", "PUT"], "/users", "functions/users.write")
api.route("ANY", "/files/{proxy+}", "functions/files.dispatch")
```

Each method + path combination must be unique — adding the same route key twice
raises an error.

`{proxy+}` is a greedy path parameter: it matches the remaining path segments and
must be the final segment. For example, `/files/{proxy+}` matches
`/files/images/avatar.png`, but not `/files` itself.

HTTP API also supports a special `$default` route that catches any request that
doesn't match another route:

```python
api.route("ANY", "$default", "functions/fallback.handler")
```

`$default` only works with `"ANY"` (or `"*"`). Any other method raises an error.

### Connecting Lambda Functions

The handler accepts the same forms as other Stelvio Lambda integrations — a path
string, a `FunctionConfig`, a dictionary, or an existing `Function` instance. See
the [REST API guide](rest-api.md#lambda-function-integration) for the full set of
options; everything there works the same way for HTTP API.

Routes that point to the same handler share a single Lambda function and
integration, and a shared function must be configured on only one of its routes —
the same rules as for [REST API routes](rest-api.md#organizing-code).

You can pass Lambda options directly when the handler is a string:

```python
api.route(
    "POST",
    "/orders",
    "functions/orders.create",
    memory=512,
    timeout=20,
    links=[users],
)
```

!!! important "30 second timeout"
    HTTP APIs cap Lambda integrations at 30 seconds. A route Lambda can have a
    longer function timeout, but API Gateway still stops waiting after 30 seconds.
    For longer work, push it onto a queue or background function.

### Lambda Event Format

HTTP APIs use Lambda payload format 2.0, which has a different shape than the v1
format used by REST APIs. The HTTP method lives under `requestContext.http.method`
and the raw path is in `rawPath`:

```python
# functions/users.py
import json

def list(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event["rawPath"]
    path_parameters = event.get("pathParameters") or {}

    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({
            "method": method,
            "path": path,
            "path_parameters": path_parameters,
        }),
    }
```

If you're migrating handlers from `RestApi`, update any code that reads v1 fields
like `event["httpMethod"]`. Path parameters remain available in
`event["pathParameters"]` in both payload formats.

## Authorization

Routes are public by default. You can protect a route with `auth=`, or set
`api.default_auth` to protect every route unless it explicitly opts out.

```python
api = HttpApi("users-api")

authorizer = api.add_lambda_authorizer(
    "session-auth",
    "functions/auth.authorize",
    identity_sources=["$request.header.Authorization"],
)

api.default_auth = authorizer

api.route("GET", "/me", "functions/me.get")
api.route("GET", "/health", "functions/health.get", auth=False)
api.route("POST", "/internal/jobs", "functions/jobs.create", auth="IAM")
```

Setting `auth=False` on a route makes it public even when a default authorizer
is configured. Passing `auth="IAM"` uses AWS IAM authorization — clients must
sign their requests with SigV4.

`api.default_auth` accepts an authorizer or `"IAM"`. It cannot be set to `False`
— leave it unset (or set it to `None`) to keep routes public by default.

### Lambda Authorizers

For custom auth logic, attach a Lambda authorizer to the API and reference it
from any number of routes:

```python
auth = api.add_lambda_authorizer(
    "api-key-auth",
    "functions/auth.api_key",
    identity_sources=[
        "$request.header.X-API-Key",
        "$request.querystring.tenant",
    ],
    ttl=300,
)

api.route("GET", "/reports", "functions/reports.list", auth=auth)
```

Identity sources use API Gateway selection expressions. Common values include
`$request.header.Authorization`, `$request.header.X-API-Key`, and
`$request.querystring.tenant`. When authorizer caching is enabled with `ttl > 0`,
API Gateway uses the configured identity sources as the cache key and rejects a
request if a required identity source is missing.

With the default `simple_response=True`, your authorizer returns the HTTP API
simple response format:

```python
# functions/auth.py
def api_key(event, context):
    api_key = event["headers"].get("x-api-key")

    return {
        "isAuthorized": api_key == "expected-key",
        "context": {"tenant": "demo"},
    }
```

If you'd rather return an IAM policy response, set `simple_response=False`.

**Configuration options:**

- `name`: Unique authorizer name within the API
- `handler`: Lambda function path, config, or `Function` instance
- `identity_sources`: List of selection expressions to extract identity from (required)
- `ttl`: Cache TTL in seconds, from `0` to `3600` (default: 300). Set to `0` to disable authorizer caching.
- `simple_response`: Return format — simple response when `True` (default), IAM policy when `False`
- `**function_config`: Additional Lambda configuration (memory, timeout, etc.)

### JWT and Cognito Authorizers

For OIDC providers, use `add_jwt_authorizer`:

```python
jwt_auth = api.add_jwt_authorizer(
    "oidc",
    issuer="https://auth.example.com/",
    audiences=["api-client-id"],
)

api.route(
    "GET",
    "/account",
    "functions/account.get",
    auth=jwt_auth,
    jwt_scopes=["account:read"],
)
```

For Stelvio Cognito components, use `add_cognito_authorizer`:

```python
from stelvio.aws.cognito import UserPool

users = UserPool("users", usernames=["email"])
web_client = users.add_client("web")

cognito_auth = api.add_cognito_authorizer(
    "cognito",
    user_pool=users,
    audiences=[web_client],
)

api.route(
    "GET",
    "/profile",
    "functions/profile.get",
    auth=cognito_auth,
    jwt_scopes=["profile:read"],
)
```

For User Pools managed outside Stelvio, pass the pool ARN and plain client ID
strings instead of components:

```python
cognito_auth = api.add_cognito_authorizer(
    "cognito",
    user_pool="arn:aws:cognito-idp:us-east-1:123456789:userpool/us-east-1_ABC123",
    audiences=["your-app-client-id"],
)
```

Do not mix forms: `UserPoolClient` audiences require a `UserPool` component (an
ARN string plus client components raises `TypeError`), and the clients must
belong to that pool.

The `jwt_scopes` argument restricts a route to tokens that include at least one
of the listed scopes. It's only valid on routes protected by a JWT or Cognito
authorizer.

**Configuration options:**

- `name`: Unique authorizer name within the API
- `issuer` / `audiences` (JWT): OIDC issuer URL and accepted `aud` claim values
- `user_pool` / `audiences` (Cognito): `UserPool` component or User Pool ARN string, and a list of `UserPoolClient` components or client ID strings
- `identity_source`: Where to read the token from (default: `"$request.header.Authorization"`)

## CORS

HTTP APIs handle CORS at the API level — Stelvio doesn't create synthetic
`OPTIONS` routes or inject CORS helpers into your Lambda functions like the REST
API component does.

For quick prototypes, `cors=True` enables permissive defaults:

```python
api = HttpApi("public-api", cors=True)
```

With `cors=True`, Stelvio allows all origins (`"*"`), all standard methods, and
all headers, with credentials disabled.

For production, configure origins, methods, and headers explicitly. `CorsConfig`
accepts either strings or lists for origins, methods, and headers; HTTP APIs
normalize them to API Gateway v2 lists. The wildcard `"*"` must be a plain
string — `allow_origins=["*"]` raises; use `allow_origins="*"` instead.

```python
from stelvio.aws.api_gateway import CorsConfig, HttpApi

api = HttpApi(
    "app-api",
    cors=CorsConfig(
        allow_origins=[
            "https://app.example.com",
            "https://admin.example.com",
        ],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["content-type", "authorization"],
        expose_headers=["x-request-id"],
        allow_credentials=True,
        max_age=3600,
    ),
)
```

When `allow_credentials=True`, list explicit origins instead of using the
wildcard `"*"`. Use `expose_headers` when the browser must read a custom
response header.

## Custom Domains

For a single API on a single domain, pass `domain_name` directly:

```python
api = HttpApi(
    "public-api",
    domain_name="api.example.com",
    cors=True,
)
```

Custom domains require a DNS provider configured on your Stelvio app — see the
[DNS guide](../../concepts/dns.md). By default, Stelvio creates the ACM
certificate, validates it with DNS, creates the API Gateway domain, and
publishes the DNS record.

Pass a `domain` component when multiple HTTP APIs should share one domain. Each
API on the shared domain must use a distinct `api_mapping_key`; the root mapping
uses no key.

```python
from stelvio.aws.api_gateway import ApiDomain, HttpApi

domain = ApiDomain("public-domain", domain_name="api.example.com")

public_api = HttpApi("public-api", domain=domain)
admin_api = HttpApi("admin-api", domain=domain, api_mapping_key="admin")
partner_api = HttpApi("partner-api", domain=domain, api_mapping_key="partners/v1")
```

This serves `public_api` at `https://api.example.com`, `admin_api` at
`https://api.example.com/admin`, and `partner_api` at
`https://api.example.com/partners/v1`.

To use an existing ACM certificate (for example a wildcard already in the
account), pass `certificate_arn` on `ApiDomain`:

```python
domain = ApiDomain(
    "public-domain",
    domain_name="api.example.com",
    certificate_arn="arn:aws:acm:us-east-1:123456789012:certificate/abc123",
)
```

!!! tip "Disable the default endpoint"
    Set `disable_execute_api_endpoint=True` when all clients should use your
    custom domain. AWS will then reject requests to the default
    `execute-api` hostname.

!!! note "Shared-domain mapping keys"
    Only one HTTP API can use the root mapping for a shared `ApiDomain`. Give
    every additional API on that domain a unique `api_mapping_key`.

    Mapping keys can contain `/` to create nested paths, such as `partners/v1`,
    but cannot start or end with `/`.

## Access Logs

`HttpApi` enables access logging by default with a 30-day retention. You can
change the retention or keep logs indefinitely:

```python
# Keep logs for 90 days
api = HttpApi("audit-api", access_log_retention_days=90)

# Keep logs indefinitely
api = HttpApi("audit-api", access_log_retention_days="forever")
```

Logs are written in JSON and include request ID, source IP, request time,
method, route key, status, protocol, response length, and any integration error
message.

## Linking

Link an `HttpApi` to a function when that function needs to call the API or build
callback URLs from it:

```python
from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.function import Function

api = HttpApi("users-api")
api.route("GET", "/users", "functions/users.list")

worker = Function(
    "worker",
    handler="functions/worker.handler",
    links=[api],
)
```

For an API named `users-api`, the linked function receives these properties:

| `stlv_resources` property | Environment variable | Description |
|---------------------------|----------------------|-------------|
| `Resources.users_api.api_url` | `STLV_USERS_API_API_URL` | Base URL for the API, including the mapping key when configured. |
| `Resources.users_api.api_execution_arn` | `STLV_USERS_API_API_EXECUTION_ARN` | API Gateway execution ARN for IAM policies and integrations. |

```python
# functions/worker.py
from stlv_resources import Resources

users_url = Resources.users_api.api_url
```

## Customization

`HttpApi` and `ApiDomain` support the `customize` parameter for overriding
properties on the underlying Pulumi resources. For an overview of how
customization works, see the [Customization guide](../../concepts/customization.md).

### Resource Keys

| Component | Resource Key | Pulumi Args Type | Description |
|-----------|--------------|------------------|-------------|
| `HttpApi` | `api` | [ApiArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/api/#inputs) | The API Gateway v2 HTTP API. |
| `HttpApi` | `stage` | [StageArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/stage/#inputs) | The auto-deploy stage. |
| `HttpApi` | `log_group` | [LogGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/cloudwatch/loggroup/#inputs) | The CloudWatch access log group. |
| `HttpApi` | `api_mapping` | [ApiMappingArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/apimapping/#inputs) | The custom domain mapping when `domain_name` or `domain` is set. |
| `ApiDomain` | `certificate` | [CertificateArgs](https://www.pulumi.com/registry/packages/aws/api-docs/acm/certificate/#inputs) | The ACM certificate for the custom domain. |
| `ApiDomain` | `domain` | [DomainNameArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/domainname/#inputs) | The API Gateway v2 custom domain. |
| `ApiDomain` | `dns_record` | DNS provider record args | The DNS record pointing the custom domain to API Gateway. |

## Next Steps

- [Working with REST API](rest-api.md) - Build on the older REST API component when you need REST-only features.
- [Working with Lambda Functions](lambda.md) - Learn how Lambda packaging and configuration work.
- [Authentication with Cognito](cognito.md) - Create user pools for JWT authorizers.
- [Linking](../../concepts/linking.md) - Learn how links generate environment variables and permissions.
