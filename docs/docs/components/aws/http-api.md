# Working with HTTP APIs in Stelvio

Stelvio supports [Amazon API Gateway HTTP APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/http-api.html) using the `HttpApi` component. HTTP APIs are API Gateway v2: they use the Lambda payload format 2.0, native CORS, auto-deploy stages, and regional endpoints.

Use `HttpApi` for new Lambda-backed HTTP endpoints unless you need a REST API v1 feature such as edge-optimized endpoints, API Gateway REST gateway responses, or non-Lambda integrations. The existing [`Api`](api-gateway.md) component remains available for REST APIs.

## Creating an HTTP API

Create an API in `stlv_app.py`, then add routes before accessing API properties:

```python
from stelvio.aws.dynamo_db import DynamoTable
from stelvio.aws.http_api import HttpApi

users = DynamoTable("users", fields={"id": "string"}, partition_key="id")

api = HttpApi("users-api", cors=True)

api.route("GET", "/users", "functions/users.list")
api.route("POST", "/users", "functions/users.create", links=[users])
api.route(["GET", "DELETE"], "/users/{id}", "functions/users.detail")
```

This creates an HTTP API, an auto-deploy stage, a CloudWatch access-log group, Lambda integrations, routes, Lambda invoke permissions, and any custom domain resources you configure.

!!! warning "Add routes and authorizers first"
    Add all routes and authorizers before accessing `.resources`, `api.url`, `api.api_id`, `api.api_arn`, or `api.execution_arn`. Resource access creates the Pulumi resources, after which route and authorizer changes are rejected.

## Configuration

You can pass options directly or use `HttpApiConfig`:

```python
from stelvio.aws.http_api import HttpApi, HttpApiConfig

# Keyword options
api = HttpApi(
    "users-api",
    domain_name="api.example.com",
    cors=True,
    access_log_retention_days=30,
)

# Or a config object
api = HttpApi(
    "users-api",
    config=HttpApiConfig(
        stage_name="$default",
        cors=True,
    ),
)
```

| Option | Default | Description |
|--------|---------|-------------|
| `domain_name` | `None` | Custom domain owned by this API, such as `api.example.com`. Requires a DNS provider in the Stelvio app. |
| `domain` | `None` | Shared `HttpApiDomain` component. Use this when multiple HTTP APIs share one domain. |
| `api_mapping_key` | `None` | Base path on a custom domain, such as `v1` or `admin`. Requires `domain_name` or `domain`. |
| `stage_name` | `"$default"` | Stage name. Use `"$default"` for no stage path in the execute-api URL. Custom names may contain letters, numbers, `_`, and `-`. |
| `cors` | `None` | `True`, `CorsConfig`, or a dictionary. See [CORS](#cors). |
| `disable_execute_api_endpoint` | `False` | Disable the default execute-api endpoint. Requires a custom domain. |
| `access_log_retention_days` | `30` | CloudWatch access-log retention in days. Use `None` to keep logs indefinitely. |

### URLs

`api.url` returns the public base URL as a Pulumi `Output[str]`.

| Configuration | URL form |
|---------------|----------|
| No domain, `$default` stage | `https://{api_id}.execute-api.{region}.amazonaws.com` |
| No domain, custom stage | `https://{api_id}.execute-api.{region}.amazonaws.com/{stage_name}` |
| `domain_name="api.example.com"` | `https://api.example.com` |
| Domain with `api_mapping_key="v2"` | `https://api.example.com/v2` |

## Defining Routes

Routes use the same basic shape as the REST API component:

```python
api.route(http_method, path, handler)
```

HTTP methods are case-insensitive and can be a single method, a list of methods, or `"ANY"`:

```python
api.route("GET", "/users", "functions/users.list")
api.route(["POST", "PUT"], "/users", "functions/users.write")
api.route("ANY", "/files/{proxy+}", "functions/files.dispatch")
api.route("*", "$default", "functions/fallback.handler")
```

Paths must start with `/`, except for the special `$default` route. `$default` can only be used with `"ANY"` or `"*"`.

### Lambda Handlers

The route handler accepts the same forms as other Stelvio Lambda integrations:

```python
from stelvio.aws.function import Function, FunctionConfig

# Handler path string
api.route("GET", "/users", "functions/users.list")

# FunctionConfig
api.route(
    "POST",
    "/users",
    FunctionConfig(
        handler="functions/users.create",
        memory=512,
        timeout=20,
    ),
)

# Dictionary
api.route(
    "PATCH",
    "/users/{id}",
    {"handler": "functions/users.update", "memory": 512},
)

# Existing Function instance
users_fn = Function("users-handler", handler="functions/users.handler")
api.route("GET", "/users", users_fn)
api.route("POST", "/users", users_fn)
```

You can also pass Lambda options directly when the handler is a string:

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

!!! important "30 second timeout limit"
    HTTP APIs cap Lambda proxy integrations at 30 seconds. Stelvio validates route Lambda timeouts and raises an error if a route uses `timeout > 30`.

### Lambda Event Format

HTTP APIs use Lambda payload format 2.0. This differs from the REST API component's v1 event shape.

```python
# functions/users.py
import json

def list(event, context):
    method = event["requestContext"]["http"]["method"]
    path = event["rawPath"]

    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps({"method": method, "path": path}),
    }
```

If you migrate from `Api`, update handlers that read v1 fields such as `event["httpMethod"]` or `event["pathParameters"]` directly.

### Migrating Route Auth From `Api`

HTTP API authorizers use JWT terminology for Cognito-backed scopes. If existing REST API code used `cognito_scopes=`, switch to `jwt_scopes=` on `HttpApi` routes:

```python
api.route(
    "GET",
    "/profile",
    "functions/profile.get",
    auth=cognito_auth,
    jwt_scopes=["profile:read"],
)
```

Stelvio still accepts `cognito_scopes=` for compatibility and emits a deprecation warning, but new HTTP API routes should use `jwt_scopes=`.

## Authorization

Routes are public by default. You can protect individual routes with `auth=`, or set `api.default_auth` to protect routes unless they opt out.

```python
api = HttpApi("users-api")

authorizer = api.add_lambda_authorizer(
    "session-auth",
    "functions/auth.authorize",
    identity_sources="$request.header.Authorization",
)

api.default_auth = authorizer

api.route("GET", "/me", "functions/me.get")
api.route("GET", "/health", "functions/health.get", auth=False)
api.route("POST", "/internal/jobs", "functions/jobs.create", auth="IAM")
```

| Route `auth` value | Result |
|--------------------|--------|
| omitted / `None` | Use `api.default_auth`. If no default is set, the route is public. |
| `False` | Public route, even when `default_auth` is set. |
| `"IAM"` | AWS IAM authorization. Clients must sign requests with SigV4. |
| Authorizer object | Use that Lambda or JWT authorizer for this route. |

### Lambda Authorizers

HTTP APIs have one Lambda authorizer type: `REQUEST`. Stelvio exposes it as `add_lambda_authorizer`.

```python
auth = api.add_lambda_authorizer(
    "api-key-auth",
    "functions/auth.api_key",
    identity_sources=[
        "$request.header.X-API-Key",
        "$request.querystring.tenant",
    ],
    ttl=300,
    simple_response=True,
)

api.route("GET", "/reports", "functions/reports.list", auth=auth)
```

With `simple_response=True`, the authorizer returns the HTTP API simple response format:

```python
# functions/auth.py
def api_key(event, context):
    api_key = event["headers"].get("x-api-key")

    return {
        "isAuthorized": api_key == "expected-key",
        "context": {"tenant": "demo"},
    }
```

Set `simple_response=False` if your authorizer returns an IAM policy response.

### JWT and Cognito Authorizers

Use `add_jwt_authorizer` for generic OIDC providers:

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

Use `add_cognito_authorizer` with Stelvio Cognito components:

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

!!! note "Identity source syntax"
    HTTP APIs use v2 identity sources such as `$request.header.Authorization`. Stelvio can rewrite common REST API v1 sources like `method.request.header.Authorization`, but new code should use the v2 form directly.

## CORS

HTTP APIs use native API-level CORS. Stelvio does not create synthetic `OPTIONS` routes and does not inject CORS response helpers into your Lambda functions.

```python
from stelvio.aws.cors import CorsConfig
from stelvio.aws.http_api import HttpApi

# Permissive defaults
api = HttpApi("public-api", cors=True)

# Explicit configuration
api = HttpApi(
    "app-api",
    cors=CorsConfig(
        allow_origins=[
            "https://app.example.com",
            "https://admin.example.com",
        ],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["content-type", "authorization"],
        allow_credentials=True,
        max_age=3600,
    ),
)
```

When `cors=True`, Stelvio uses `allow_origins="*"`, `allow_methods="*"`, and `allow_headers="*"`. If you set `allow_credentials=True`, list explicit origins instead of `"*"`.

## Custom Domains

For one API on one domain, pass `domain_name` directly:

```python
from stelvio.aws.http_api import HttpApi

api = HttpApi(
    "public-api",
    domain_name="api.example.com",
    cors=True,
)
```

As described in the [DNS guide](../../concepts/dns.md), custom domains require a DNS provider on the Stelvio app. Stelvio creates the ACM certificate, validates it with DNS, creates the API Gateway v2 domain, and publishes the DNS record.

For multiple HTTP APIs on one domain, create a shared `HttpApiDomain` and give each API a distinct mapping key:

```python
from stelvio.aws.http_api import HttpApi, HttpApiDomain

domain = HttpApiDomain("public-domain", domain_name="api.example.com")

public_api = HttpApi("public-api", domain=domain)
admin_api = HttpApi("admin-api", domain=domain, api_mapping_key="admin")
partner_api = HttpApi("partner-api", domain=domain, api_mapping_key="partners/v1")
```

This gives you:

| API | URL |
|-----|-----|
| `public_api` | `https://api.example.com` |
| `admin_api` | `https://api.example.com/admin` |
| `partner_api` | `https://api.example.com/partners/v1` |

!!! tip "Disable execute-api for domain-only APIs"
    Set `disable_execute_api_endpoint=True` when all clients should use your custom domain. AWS will reject requests to the default execute-api hostname.

## Linking

`HttpApi` is linkable. Linking an HTTP API to a Lambda function gives the function the API URL and execution ARN as environment variables and generated `stlv_resources.py` properties.

```python
from stelvio.aws.function import Function
from stelvio.aws.http_api import HttpApi

api = HttpApi("billing-api")
api.route("POST", "/invoices", "functions/invoices.create")

worker = Function(
    "billing-worker",
    handler="functions/worker.handler",
    links=[api],
)
```

```python
# functions/worker.py
from stlv_resources import Resources

def handler(event, context):
    api_url = Resources.billing_api.url
    execution_arn = Resources.billing_api.execution_arn
```

| Property | Environment variable | Description |
|----------|----------------------|-------------|
| `url` | `STLV_<API_NAME>_URL` | Public base URL for the API. |
| `execution_arn` | `STLV_<API_NAME>_EXECUTION_ARN` | Execute API ARN, useful when granting `execute-api:Invoke`. |

Linking an HTTP API grants no IAM permissions by default. For IAM-protected routes, grant callers `execute-api:Invoke` for the required route ARN.

## REST API vs HTTP API

The `Api` and `HttpApi` components are separate because API Gateway REST APIs and HTTP APIs behave differently.

| Concern | `Api` REST API | `HttpApi` HTTP API |
|---------|----------------|--------------------|
| Import | `from stelvio.aws.api_gateway import Api` | `from stelvio.aws.http_api import HttpApi` |
| Lambda payload | 1.0 | 2.0 |
| Stage behavior | Explicit deployment and stage, default `"v1"` | Auto-deploy stage, default `"$default"` |
| CORS | Stelvio creates `OPTIONS` routes and Lambda CORS helpers | Native API-level CORS |
| Origins | Single origin in Stelvio REST CORS | Multiple origins supported |
| Endpoints | Regional or edge-optimized | Regional only |
| Custom domains | `BasePathMapping` | `ApiMapping`, with shareable `HttpApiDomain` |
| Authorizers | Token, request, Cognito, IAM | Lambda request, JWT/Cognito, IAM |
| Integration timeout | 29 seconds | 30 seconds |

Choose `Api` when you need a REST API-only feature. Choose `HttpApi` for native HTTP API behavior, especially payload format 2.0, multi-origin CORS, auto-deploy stages, or shared domain mappings.

## Access Logs

`HttpApi` creates a dedicated CloudWatch log group and enables stage access logs by default. The default retention is 30 days:

```python
# Keep logs for 90 days
api = HttpApi("audit-api", access_log_retention_days=90)

# Keep logs indefinitely
api = HttpApi("audit-api", access_log_retention_days=None)
```

The default log format is JSON and includes request ID, source IP, request time, method, route key, status, protocol, response length, and integration error message.

## Customization

The `HttpApi` and `HttpApiDomain` components support the `customize` parameter to override underlying Pulumi resource properties. For an overview, see the [Customization guide](../../concepts/customization.md).

### HttpApi Resource Keys

| Resource Key | Pulumi Args Type | Description |
|--------------|------------------|-------------|
| `api` | [ApiArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/api/#inputs) | The API Gateway v2 HTTP API. |
| `stage` | [StageArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/stage/#inputs) | The auto-deploy API stage. |
| `log_group` | [LogGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/cloudwatch/loggroup/#inputs) | The CloudWatch access-log group. |
| `api_mapping` | [ApiMappingArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/apimapping/#inputs) | The custom-domain API mapping, when a domain is configured. |

### HttpApiDomain Resource Keys

| Resource Key | Pulumi Args Type | Description |
|--------------|------------------|-------------|
| `certificate` | [CertificateArgs](https://www.pulumi.com/registry/packages/aws/api-docs/acm/certificate/#inputs) | The ACM certificate created by `AcmValidatedDomain`. |
| `custom_domain` | [DomainNameArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/domainname/#inputs) | The API Gateway v2 domain name. |
| `dns_record` | DNS provider record args | The DNS record pointing the domain to API Gateway. |

### Example

```python
api = HttpApi(
    "users-api",
    customize={
        "api": {
            "description": "Users HTTP API",
        },
        "stage": {
            "route_settings": [
                {
                    "route_key": "GET /users",
                    "throttling_burst_limit": 100,
                    "throttling_rate_limit": 50,
                }
            ],
        },
        "log_group": {
            "retention_in_days": 90,
        },
    },
)
```

Use route-key strings such as `GET /users`, `ANY /files/{proxy+}`, or `$default` when customizing stage route settings.

## Best Practices

- Prefer the default `$default` stage unless you specifically need stage names in URLs.
- Use `cors=True` for public prototypes, then restrict origins before production.
- Use `HttpApiDomain` when more than one API should live under the same domain.
- Keep route Lambda timeouts at or below 30 seconds and move long-running work to queues or background functions.
- Use `auth=False` for public health checks when `default_auth` protects the rest of the API.

## Next Steps

- [Working with API Gateway](api-gateway.md) - Compare the REST API component.
- [Working with Lambda Functions](lambda.md) - Learn how Lambda packaging and configuration work.
- [Authentication with Cognito](cognito.md) - Create user pools and app clients for JWT authorizers.
- [Linking](../../concepts/linking.md) - Learn how links generate environment variables and permissions.
- [Customization](../../concepts/customization.md) - Override underlying Pulumi resource properties.