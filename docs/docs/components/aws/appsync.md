# Working with AppSync in Stelvio

Stelvio supports creating and managing [AWS AppSync](https://aws.amazon.com/appsync/) GraphQL APIs using the `AppSync` component. AppSync is a managed GraphQL service that connects your API to data sources like Lambda functions, DynamoDB tables, HTTP endpoints, and more.

## When to Use AppSync vs API Gateway

Use **AppSync** when you need:

- A GraphQL API with schema-first design
- Real-time subscriptions (WebSocket-based)
- Multiple data sources resolved from a single query
- Per-field authorization with schema directives

Use **API Gateway** when you need:

- REST endpoints
- Simple request/response patterns
- WebSocket APIs with custom protocols

## Quick Start

The most common pattern — Lambda resolvers with [AWS Lambda PowerTools](https://docs.powertools.aws.dev/lambda/python/latest/core/event_handler/appsync/):

```graphql title="schema.graphql"
type Query {
    getPost(id: ID!): Post
    listPosts: [Post]
}

type Mutation {
    createPost(title: String!, content: String!): Post
}

type Post {
    id: ID!
    title: String!
    content: String!
}
```

```python
from stelvio.aws.appsync import AppSync, CognitoAuth, ApiKeyAuth

api = AppSync("myapi", schema="schema.graphql",
    auth=CognitoAuth(user_pool_id="us-east-1_ABC123"),
    additional_auth=["iam", ApiKeyAuth()],
)

posts = api.data_source_lambda("posts", handler="resolvers/posts.handler")
api.query("getPost", posts)
api.query("listPosts", posts)
api.mutation("createPost", posts)
```

One Lambda handles all three resolvers using the PowerTools router pattern. The data source is defined once and resolvers reference it. AppSync uses Direct Lambda Resolver — no JavaScript mapping code needed.

Your Lambda handler with PowerTools:

```python
# resolvers/posts.py
from aws_lambda_powertools.event_handler import AppSyncResolver

app = AppSyncResolver()

@app.resolver(type_name="Query", field_name="getPost")
def get_post(id: str):
    # fetch from database...
    return {"id": id, "title": "My Post"}

@app.resolver(type_name="Query", field_name="listPosts")
def list_posts():
    # fetch all posts...
    return [{"id": "1", "title": "First Post"}]

@app.resolver(type_name="Mutation", field_name="createPost")
def create_post(title: str, content: str):
    # save to database...
    return {"id": "new-id", "title": title, "content": content}

def handler(event, context):
    return app.resolve(event, context)
```

## Schema

The `schema` parameter accepts either a file path (relative to project root) or an inline SDL string:

```python
# File path — reads from project root
api = AppSync("myapi", schema="schema.graphql", auth=...)

# Inline SDL string
api = AppSync("myapi", schema="""
    type Query {
        getPost(id: ID!): Post
    }
    type Post {
        id: ID!
        title: String!
    }
""", auth=...)
```

Stelvio treats values ending in `.graphql` or `.gql` as file paths. If that file is missing, it raises `FileNotFoundError`. Other values are treated as inline SDL strings.

## Component Lifecycle

`AppSync`, `AppSyncDataSource`, `AppSyncResolver`, and `PipeFunction` are all Stelvio components with lazy resource creation.

- Builder methods (`data_source_*`, `query`/`mutation`/`subscription`/`resolver`, `pipe_function`) register child components.
- Actual AWS resources are created during deployment.

## Authentication

AppSync supports five authentication modes. Set one as the default and optionally add more for multi-auth.

`AuthConfig` is a union type:

```python
AuthConfig = Literal["iam"] | ApiKeyAuth | CognitoAuth | OidcAuth | LambdaAuth
```

### IAM

No configuration needed — pass the string `"iam"`:

```python
api = AppSync("myapi", schema="schema.graphql", auth="iam")
```

Clients sign requests with AWS Signature V4. Best for service-to-service communication.

### API Key

```python
from stelvio.aws.appsync import ApiKeyAuth

api = AppSync("myapi", schema="schema.graphql", auth=ApiKeyAuth())
api = AppSync("myapi", schema="schema.graphql", auth=ApiKeyAuth(expires=90))  # 90 days
```

| Option    | Default | Description                                 |
|-----------|---------|---------------------------------------------|
| `expires` | `365`   | Days until the API key expires (1–365)      |

Stelvio auto-creates the API Key resource. Access the key value via `api.api_key`.

### Cognito User Pools

```python
from stelvio.aws.appsync import CognitoAuth

api = AppSync("myapi", schema="schema.graphql",
    auth=CognitoAuth(user_pool_id="us-east-1_ABC123"),
)
```

| Option                 | Default | Description                                    |
|------------------------|---------|------------------------------------------------|
| `user_pool_id`         | —       | Cognito User Pool ID (required)                |
| `region`               | `None`  | AWS region of the user pool (defaults to stack) |
| `app_id_client_regex`  | `None`  | Regex to match against client ID in JWT token  |

### OpenID Connect

```python
from stelvio.aws.appsync import OidcAuth

api = AppSync("myapi", schema="schema.graphql",
    auth=OidcAuth(issuer="https://auth.example.com"),
)
```

| Option      | Default | Description                             |
|-------------|---------|---------------------------------------- |
| `issuer`    | —       | OIDC issuer URL (required)              |
| `client_id` | `None`  | Client ID to validate against aud claim |
| `auth_ttl`  | `None`  | Token expiration TTL in milliseconds    |
| `iat_ttl`   | `None`  | Token issued-at TTL in milliseconds     |

### Lambda Authorizer

```python
from stelvio.aws.appsync import LambdaAuth

api = AppSync("myapi", schema="schema.graphql",
    auth=LambdaAuth(handler="resolvers/auth.handler"),
)

# With function options
api = AppSync("myapi", schema="schema.graphql",
    auth=LambdaAuth(
        handler="resolvers/auth.handler",
        links=[users_table],
        memory=256,
        result_ttl=300,
    ),
)
```

| Option                            | Default | Description                                      |
|-----------------------------------|---------|--------------------------------------------------|
| `handler`                         | —       | Handler as string, `FunctionConfig`, or `Function` (required) |
| `result_ttl`                      | `None`  | Authorization result cache TTL in seconds        |
| `identity_validation_expression`  | `None`  | Regex to validate the authorization token        |

For additional function options like `memory`, `timeout`, `links`, and `environment`, see [Lambda Functions](lambda.md). These are convenience options — if you pass a `FunctionConfig` or `Function` instance, configure them on the handler directly.

### Multi-Auth

Set one default mode and optionally add more. Per-field control uses schema directives.

```python
api = AppSync("myapi", schema="schema.graphql",
    auth=CognitoAuth(user_pool_id="..."),       # default for all fields
    additional_auth=["iam", ApiKeyAuth()],       # enables @aws_iam and @aws_api_key
)
```

A directive (`@aws_iam`, `@aws_api_key`, `@aws_cognito_user_pools`, `@aws_oidc`) can only be used if that auth mode is configured as the default or in `additional_auth`. Fields without directives use the default auth mode.

```graphql
type Query {
    # Uses default auth (Cognito)
    getMyProfile: User

    # Accessible with IAM or API key
    getPublicPost(id: ID!): Post @aws_iam @aws_api_key
}
```

!!! warning "Shared return types need both directives"
    If a return type is used by both Cognito and IAM callers, it needs both directives on the type itself:

    ```graphql
    type Post @aws_iam @aws_cognito_user_pools {
        id: ID!
        title: String!
    }
    ```

    Without this, callers with one auth mode may get null for that type, even if they can access the field.

## Data Sources

Each data source type has its own method. Each returns an `AppSyncDataSource` object that you pass to resolver methods.

All data source methods accept these common parameters:

- **`name`** — Data source name (unique within this API)
- **`customize`** — Customization dict for `data_source` and `service_role` sub-resources

The tables below show only type-specific parameters.

### Lambda

The most common data source. One Lambda can handle multiple resolvers using PowerTools' router pattern.

```python
posts = api.data_source_lambda("posts",
    handler="resolvers/posts.handler",
)
```

| Param        | Description                                             |
|--------------|---------------------------------------------------------|
| `handler`    | Handler as string, `FunctionConfig`, or `Function`      |
| `**fn_opts`  | Function options — `links`, `memory`, `timeout`, etc. See [Lambda Functions](lambda.md) |

You can also pass a pre-built `Function` instance:

```python
from stelvio.aws.function import Function

posts_fn = Function("posts-fn", handler="resolvers/posts.handler", memory=512)
posts = api.data_source_lambda("posts", posts_fn)
```

### DynamoDB

Connects AppSync directly to a DynamoDB table. Requires JavaScript code to specify the operation.

```python
from stelvio.aws.dynamo_db import DynamoTable, AttributeType

items_table = DynamoTable("items",
    fields={"id": AttributeType.STRING},
    partition_key="id",
)

items = api.data_source_dynamo("items", table=items_table)

# DynamoDB resolvers require JS code
api.query("getItem", items, code="resolvers/getItem.js")
```

| Param       | Description                                         |
|-------------|-----------------------------------------------------|
| `table`     | Stelvio DynamoDB component instance                 |

!!! info "Stelvio components only"
    `data_source_dynamo` requires a Stelvio `DynamoTable` component — raw ARN strings are not accepted.

!!! tip "Code generation helpers"
    Writing JavaScript for DynamoDB resolvers can be tedious. Stelvio provides [helper functions](#code-generation-helpers) that generate APPSYNC_JS code for common operations like GetItem, PutItem, Scan, and Query.

### HTTP

Connects to external HTTP endpoints.

```python
ext = api.data_source_http("ext", url="https://api.example.com")

# HTTP resolvers require JS code
api.query("fetchExternal", ext, code="resolvers/fetchExt.js")
```

Example resolver JS for an HTTP data source:

```javascript
// resolvers/fetchExt.js
export function request(ctx) {
    return {
        method: 'GET',
        resourcePath: `/items/${ctx.args.id}`,
    };
}

export function response(ctx) {
    return JSON.parse(ctx.result.body);
}
```

| Param       | Description                                         |
|-------------|-----------------------------------------------------|
| `url`       | Base URL for the HTTP endpoint                      |

!!! info "HTTP service role"
    AppSync still requires a `service_role_arn` for HTTP data sources. Stelvio creates that IAM role, but no inline data-access policy is attached because HTTP calls are made by AppSync to the configured endpoint.

### RDS (Aurora Data API)

Connects to an Aurora database through the Data API.

```python
db = api.data_source_rds("db",
    cluster_arn="arn:aws:rds:us-east-1:123456789:cluster:mydb",
    secret_arn="arn:aws:secretsmanager:us-east-1:123456789:secret:mydb-creds",
    database="mydb",
)

api.query("getUser", db, code="resolvers/getUser.js")
```

| Param         | Description                                         |
|---------------|-----------------------------------------------------|
| `cluster_arn` | Aurora cluster ARN                                  |
| `secret_arn`  | Secrets Manager secret ARN for database credentials |
| `database`    | Database name                                       |

### OpenSearch

Connects to an OpenSearch domain.

```python
search = api.data_source_opensearch("search",
    endpoint="https://search-mydomain-abc123.us-east-1.es.amazonaws.com",
)

api.query("searchItems", search, code="resolvers/searchItems.js")
```

| Param       | Description                                         |
|-------------|-----------------------------------------------------|
| `endpoint`  | OpenSearch domain endpoint URL                      |

### NONE (No Backend)

NONE means no backend call. Pass `None` as the data source in resolver methods — there's no separate method for it. See the [NONE Data Source](#none-data-source) section below for details.

### Data Source Resources

Each data source creates an IAM service role (trusted by `appsync.amazonaws.com`) with the permissions that type needs. Access the underlying resources via `.resources`:

```python
posts = api.data_source_lambda("posts", handler="resolvers/posts.handler")

# After AppSync resources are created:
posts.resources.data_source   # appsync.DataSource
posts.resources.service_role  # iam.Role
posts.resources.function      # Function (only for Lambda data sources)
```

## Resolvers

Four methods for adding resolvers:

```python
api.query(field, data_source)
api.mutation(field, data_source)
api.subscription(field, data_source)
api.resolver(type_name, field, data_source)
```

`query()`, `mutation()`, and `subscription()` are shortcuts for the most common GraphQL types. `resolver()` handles any type, including nested types.

### When Is Code Needed?

Whether you need to provide `code=` depends on the data source type:

| Data Source   | Code Required? | Reason |
|---------------|---------------|--------|
| **Lambda**    | No            | Direct Lambda Resolver forwards the full GraphQL context to your Lambda |
| **DynamoDB**  | Yes           | JS tells AppSync which operation to perform (GetItem, Query, etc.) |
| **HTTP**      | Yes           | JS specifies the HTTP method, path, and response mapping |
| **RDS**       | Yes           | JS provides the SQL query |
| **OpenSearch** | Yes          | JS specifies the search query |
| **NONE**      | No (optional) | If `code=` is omitted, Stelvio auto-generates a passthrough resolver; if `code=` is provided, Stelvio uses your custom APPSYNC_JS resolver |

The `code` parameter accepts either an inline JavaScript string or a `.js` file path (relative to project root):

```python
# Lambda — no code needed
api.query("getPost", posts)

# DynamoDB — JS required (file path)
api.query("getItem", items, code="resolvers/getItem.js")

# DynamoDB — JS required (inline)
api.query("getItem", items, code="""
import { util } from '@aws-appsync/utils';
export function request(ctx) {
    return {
        operation: 'GetItem',
        key: util.dynamodb.toMapValues({ id: ctx.args.id }),
    };
}
export function response(ctx) {
    return ctx.result;
}
""")

# NONE — autogenerated passthrough when code= is omitted
api.mutation("sendMessage", None)

# NONE — custom APPSYNC_JS when code= is provided
api.mutation("enriched", None, code="resolvers/enrich.js")
```

!!! warning "Missing code validation"
    If you use a DynamoDB, HTTP, RDS, or OpenSearch data source without providing `code=`, Stelvio raises a `ValueError` with a clear error message.

!!! info "Strict file detection"
    Stelvio treats values ending in `.graphql`/`.gql` (schema inputs) and `.js` (resolver/function code) as file paths. If the file does not exist, it raises `FileNotFoundError`. Other values are treated as inline strings.

### Nested Type Resolvers

Use `resolver()` for fields on types other than Query, Mutation, or Subscription:

```python
# Resolve the 'author' field on Post type
api.resolver("Post", "author", users_ds)
```

### Resolver Resources

Each resolver returns an `AppSyncResolver` with `.resources`:

```python
r = api.query("getPost", posts)
# After AppSync resources are created:
r.resources.resolver  # appsync.Resolver
```

## NONE Data Source

NONE means "no backend call." Pass `None` as the data source in resolver methods.

When no `code=` is provided, Stelvio generates a passthrough:

```javascript
export function request(ctx) {
    return { payload: ctx.args };
}

export function response(ctx) {
    return ctx.result;
}
```

### Real-Time Pub/Sub Pattern

The primary use case for NONE is **real-time messaging without persistence**. A mutation with NONE passes args through as the "result." Any `@aws_subscribe` subscription watching that mutation receives it automatically — no database, no Lambda, pure broadcast.

```graphql
type Mutation {
    sendMessage(channel: String!, content: String!): Message
}

type Subscription {
    onMessage(channel: String!): Message
        @aws_subscribe(mutations: ["sendMessage"])
}

type Message {
    channel: String!
    content: String!
}
```

```python
# No code needed — passthrough forwards args as result
api.mutation("sendMessage", None)

# No infra needed for basic subscriptions — @aws_subscribe handles it
```

When a client calls `sendMessage`, AppSync:

1. Runs the passthrough (returns `ctx.args` as the result)
2. Pushes the result to all clients subscribed to `onMessage` matching the channel

For custom behavior (timestamp injection, identity enrichment), provide `code=`:

```python
api.mutation("sendMessage", None, code="""
export function request(ctx) {
    return {
        payload: {
            ...ctx.args,
            sentAt: util.time.nowISO8601(),
            sender: ctx.identity.username,
        },
    };
}

export function response(ctx) {
    return ctx.result;
}
""")
```

## Pipeline Resolvers

Pipelines chain multiple steps in sequence. Each step is an AppSync Function (an AWS-specific concept — not a Lambda function). Each step has its own data source and JavaScript code. Steps read the previous result via `ctx.prev.result` and share data via `ctx.stash`.

```python
auth_step   = api.pipe_function("checkAuth", None, code="resolvers/auth.js")
delete_step = api.pipe_function("doDelete", items, code="resolvers/delete.js")

api.mutation("deletePost", [auth_step, delete_step])
```

A common pattern: auth check (NONE data source, just inspects `ctx.identity`) followed by business logic (real data source).

### pipe_function

```python
api.pipe_function(name, data_source, *, code, customize=None)
```

| Param        | Description                                              |
|--------------|----------------------------------------------------------|
| `name`       | Pipeline function name (unique within this API)          |
| `data_source`| Which backend this step talks to, or `None` for NONE     |
| `code`       | APPSYNC_JS code — inline string or `.js` file path (required) |
| `customize`  | Customization for the AppSync Function resource           |

Returns a `PipeFunction` with `.resources` exposing the underlying `appsync.Function`.

Example auth check step:

```javascript
// resolvers/auth.js
export function request(ctx) {
    const isAdmin = ctx.identity.groups?.includes('admin');
    if (!isAdmin) {
        util.unauthorized();
    }
    return { payload: null };
}

export function response(ctx) {
    return ctx.prev.result;
}
```

## Subscriptions

AppSync has two subscription patterns:

### Basic Subscriptions

Use the `@aws_subscribe` directive in your schema. No Stelvio infrastructure code needed:

```graphql
type Subscription {
    onCreatePost: Post @aws_subscribe(mutations: ["createPost"])
}
```

AppSync automatically pushes mutation results to subscribed clients over WebSocket. This works with any mutation — Lambda, DynamoDB, NONE, etc.

### Enhanced Subscriptions

For server-side filtering (clients only receive matching events), use the `subscription()` method:

```python
api.subscription("onCreatePost", None, code="resolvers/filterPosts.js")
```

The JS code runs server-side to filter which events reach each subscriber.

## Code Generation Helpers

Writing JavaScript for DynamoDB resolvers can be tedious. Stelvio provides helper functions that generate APPSYNC_JS code for common DynamoDB operations:

```python
from stelvio.aws.appsync import dynamo_get, dynamo_scan, dynamo_put, dynamo_remove, dynamo_query

api.query("getItem", items, code=dynamo_get("id"))
api.query("listItems", items, code=dynamo_scan())
api.mutation("createItem", items, code=dynamo_put())
api.mutation("deleteItem", items, code=dynamo_remove("id"))
```

### Available Helpers

**`dynamo_get(pk, sk=None)`** — GetItem operation:

```python
# Single key
api.query("getItem", items, code=dynamo_get("id"))

# Compound key
api.query("getByKeys", items, code=dynamo_get(pk="userId", sk="postId"))
```

**`dynamo_put(key_fields=None)`** — PutItem operation. All mutation arguments become item attributes.

```python
# Auto-generated ID — generates a unique id as the partition key
api.mutation("createItem", items, code=dynamo_put())

# Explicit key fields — these args are extracted for the DynamoDB key,
# and all args become item attributes
api.mutation("createItem", items, code=dynamo_put(key_fields=["userId", "postId"]))
```

Without `key_fields`, an auto-generated UUID is used as the partition key (`id`). With `key_fields`, those arguments are extracted for the DynamoDB key and all arguments become item attributes.

**`dynamo_scan(limit=None, next_token_arg="nextToken")`** — Scan operation with optional pagination.

```python
api.query("listItems", items, code=dynamo_scan())
api.query("listItems", items, code=dynamo_scan(limit=20))
```

`limit` caps the number of items returned per page. `next_token_arg` is the name of the GraphQL argument used for pagination (defaults to `"nextToken"`). Your GraphQL schema needs a matching argument on the query field to support pagination.

**`dynamo_query(pk_field, sk_condition=None, sk_expression_values=None)`** — Query operation:

```python
api.query("postsByUser", items, code=dynamo_query("userId"))
api.query("recentPosts", items, code=dynamo_query("userId", sk_condition="begins_with(sk, :prefix)"))
api.query(
    "recentPosts",
    items,
    code=dynamo_query(
        "userId",
        sk_condition="begins_with(sk, :prefix)",
        sk_expression_values={":prefix": "ctx.args.prefix"},
    ),
)
```

**`dynamo_remove(pk, sk=None)`** — DeleteItem operation:

```python
api.mutation("deleteItem", items, code=dynamo_remove("id"))
api.mutation("deleteItem", items, code=dynamo_remove(pk="userId", sk="postId"))
```

These are pure functions returning JavaScript strings, so `code=` stays the universal parameter.

## Linking

### AppSync as a Linkable

Other Lambda functions can link to your AppSync API. This is useful for backend services that call mutations via IAM-signed HTTP:

```python
from stelvio.aws.function import Function

notifier = Function("notifier",
    handler="jobs/notify.handler",
    links=[api],
)
```

This gives the Lambda:

| Property  | Environment Variable     | Description             |
|-----------|--------------------------|-------------------------|
| `url`     | `STLV_MYAPI_URL`        | GraphQL endpoint URL    |
| `api_key` | `STLV_MYAPI_API_KEY`    | API key (if configured) |

Plus `appsync:GraphQL` permission on the API's ARN.

!!! info "API key in link"
    `STLV_{NAME}_API_KEY` is only included when API_KEY auth is configured (as default or additional auth mode).

### Lambda Data Source Links

Links on Lambda data sources work the standard way — nothing AppSync-specific:

```python
posts = api.data_source_lambda("posts",
    handler="resolvers/posts.handler",
    links=[table, queue],
)
```

The Lambda function gets the linked resources' environment variables and permissions automatically.

## Custom Domains

Connect a custom domain to your AppSync API. Stelvio handles ACM certificate creation and DNS records:

```python
api = AppSync("myapi", schema="schema.graphql",
    auth=CognitoAuth(user_pool_id="..."),
    domain="graphql.example.com",
)
```

This requires a DNS provider configured in your app:

```python
from stelvio import StelvioApp
from stelvio.cloudflare.dns import CloudflareDns

app = StelvioApp(
    "my-app",
    dns=CloudflareDns("your-cloudflare-zone-id"),
)
```

Behind the scenes, Stelvio creates:

- An ACM certificate for the domain
- DNS validation records for the certificate
- An AppSync `DomainName` resource
- A `DomainNameApiAssociation` linking the domain to the API
- A CNAME record pointing the domain to the AppSync endpoint

## Properties

Access these properties on the `AppSync` instance:

| Property  | Type              | Description                                       |
|-----------|-------------------|---------------------------------------------------|
| `url`     | `Output[str]`     | GraphQL endpoint URL                              |
| `arn`     | `Output[str]`     | API ARN                                           |
| `api_id`  | `Output[str]`     | API ID                                            |
| `api_key` | `Output[str] \| None` | API key value, or `None` if not configured    |

## Runtime

AppSync supports two resolver runtimes: APPSYNC_JS (JavaScript) and VTL (Velocity Template Language). Stelvio uses APPSYNC_JS exclusively — it's the modern standard and what AWS recommends for new APIs.

Stelvio does not support VTL resolvers. All resolver code must be written in APPSYNC_JS (the `code=` parameter).

## Customization

The `customize` parameter is available at every level — constructor, data sources, resolvers, and pipeline functions. Each level has its own resource keys. For an overview of how customization works, see the [Customization guide](customization.md).

**AppSync** (constructor):

| Resource Key           | Pulumi Args Type                    | Description                          |
|------------------------|-------------------------------------|--------------------------------------|
| `api`                  | GraphQLApiArgs                      | The AppSync GraphQL API              |
| `domain_name`          | DomainNameArgs                      | The custom domain                    |
| `api_key`              | dict                                | API key resource args                |
| `auth_permissions`     | PermissionArgs                      | Lambda authorizer invoke permissions |
| `acm_validated_domain` | AcmValidatedDomainCustomizationDict | ACM certificate for custom domain    |
| `domain_association`   | DomainNameApiAssociationArgs        | Domain-to-API association            |
| `domain_dns_record`    | dict                                | DNS record for the custom domain     |

**AppSyncDataSource** (data source methods):

| Resource Key   | Pulumi Args Type | Description              |
|----------------|------------------|--------------------------|
| `data_source`  | DataSourceArgs   | The AppSync data source  |
| `service_role` | RoleArgs         | IAM service role         |

**AppSyncResolver** (resolver methods):

| Resource Key | Pulumi Args Type | Description            |
|--------------|------------------|------------------------|
| `resolver`   | ResolverArgs     | The AppSync resolver   |

**PipeFunction** (`pipe_function`):

| Resource Key | Pulumi Args Type | Description                 |
|--------------|------------------|-----------------------------|
| `function`   | FunctionArgs     | The AppSync Function (step) |

```python
# Constructor-level
api = AppSync("myapi", schema="schema.graphql",
    auth="iam",
    customize={"api": {"xray_enabled": True}},
)

# Data source
items = api.data_source_dynamo("items", table=items_table, customize={
    "service_role": {"tags": {"Team": "backend"}},
})

# Resolver
api.query("getPost", posts, customize={
    "resolver": {"caching_config": {"ttl": 3600}},
})
```

!!! tip "App-level customization"
    To apply customizations to all instances of a component type, use the `customize` option in `StelvioAppConfig` with **component classes** as keys:

    ```python
    from stelvio.config import StelvioAppConfig
    from stelvio.aws.appsync import AppSync, AppSyncDataSource

    @app.config
    def configuration(env: str) -> StelvioAppConfig:
        return StelvioAppConfig(
            customize={
                AppSync: {
                    "api": {"xray_enabled": True},
                },
                AppSyncDataSource: {
                    "service_role": {"tags": {"Team": "backend"}},
                },
            }
        )
    ```

    See [Customization guide](customization.md) for details.

## Next Steps

Now that you understand AppSync, you might want to explore:

- [Working with Lambda Functions](lambda.md) - Learn more about Lambda configuration
- [Working with DynamoDB](dynamo-db.md) - Store and retrieve data
- [Linking](linking.md) - Understand how Stelvio automates IAM permissions
- [DNS and Custom Domains](dns.md) - Configure custom domains for your API
