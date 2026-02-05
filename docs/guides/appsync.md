# AWS AppSync GraphQL API

AWS AppSync is a fully managed GraphQL service that makes it easy to develop GraphQL APIs 
by handling the heavy lifting of securely connecting to data sources like AWS DynamoDB, 
Lambda, and more.

## When to Use AppSync

AppSync is ideal when you need:

- **Real-time data synchronization** - WebSocket subscriptions for live updates
- **Offline support** - Built-in conflict resolution for mobile apps
- **Multiple data sources** - Combine data from DynamoDB, Lambda, HTTP APIs, and more in a single query
- **Fine-grained authorization** - Control access at the field level with multiple auth methods
- **Reduced client complexity** - Clients fetch exactly the data they need in one request

## Creating an AppSync API

Creating a GraphQL API in Stelvio is straightforward:

```python
from stelvio.aws.appsync import AppSync

# With a schema file
api = AppSync("my-api", "schema.graphql")

# Or with an inline schema
api = AppSync("my-api", """
    type Query {
        getUser(id: ID!): User
    }
    
    type User {
        id: ID!
        name: String!
        email: String
    }
""")
```

The `schema` parameter accepts either:
- A file path (if the file exists, Stelvio loads the schema from it)
- An inline GraphQL schema string

## Authentication

By default, AppSync APIs use **API key authentication** with a key that expires in 365 days:

```python
# Default: API_KEY auth with 365-day expiration
api = AppSync("my-api", "schema.graphql")

# Custom expiration (30 days)
api = AppSync("my-api", "schema.graphql", api_key_expires=30)

# Disable API key auth (requires additional_auth)
api = AppSync(
    "my-api", 
    "schema.graphql",
    api_key_expires=0,
    additional_auth=[...]
)
```

### Additional Authentication Methods

You can add multiple authentication methods using `additional_auth`:

#### Cognito User Pools

```python
api = AppSync(
    "my-api",
    "schema.graphql",
    additional_auth=[{
        "type": "AMAZON_COGNITO_USER_POOLS",
        "user_pool_id": "us-east-1_xxxxx",
        "aws_region": "us-east-1",  # Optional, defaults to current region
        "app_id_client_regex": None,  # Optional client ID validation
    }]
)
```

#### OpenID Connect (OIDC)

```python
api = AppSync(
    "my-api",
    "schema.graphql",
    additional_auth=[{
        "type": "OPENID_CONNECT",
        "issuer": "https://auth.example.com",
        "client_id": "my-client-id",  # Optional
        "auth_ttl": 3600000,  # Token TTL in ms (optional)
        "iat_ttl": 3600000,  # Issued-at TTL in ms (optional)
    }]
)
```

#### Lambda Authorizer

```python
api = AppSync(
    "my-api",
    "schema.graphql",
    additional_auth=[{
        "type": "AWS_LAMBDA",
        "handler": "functions/authorizer.handler",
        "identity_validation_expression": "^Bearer ",  # Optional
        "authorizer_result_ttl": 300,  # Cache TTL in seconds (optional)
    }]
)
```

#### Multiple Auth Methods

Combine authentication methods:

```python
api = AppSync(
    "my-api",
    "schema.graphql",
    additional_auth=[
        {
            "type": "AMAZON_COGNITO_USER_POOLS",
            "user_pool_id": "us-east-1_xxxxx",
        },
        {
            "type": "OPENID_CONNECT",
            "issuer": "https://auth.example.com",
        },
    ]
)
```

## Data Sources

Data sources connect your GraphQL API to backend services. AppSync supports multiple data source types.

### Lambda Data Source

Connect to AWS Lambda functions for custom business logic:

```python
api = AppSync("my-api", "schema.graphql")

# Simple handler path
api.add_data_source("users", handler="functions/users.handler")

# With Lambda configuration
api.add_data_source(
    "users",
    handler="functions/users.handler",
    memory=512,
    timeout=30,
)

# Using an existing Function
from stelvio.aws.function import Function

users_fn = Function("users-handler", handler="functions/users.handler")
api.add_data_source("users", handler=users_fn)
```

### DynamoDB Data Source

Direct integration with DynamoDB tables:

```python
api.add_data_source("users-table", dynamodb="users")

# With custom region
api.add_data_source(
    "users-table", 
    dynamodb="users",
    dynamodb_region="eu-west-1"
)
```

### HTTP Data Source

Connect to external REST APIs:

```python
api.add_data_source("rest-api", http="https://api.example.com")
```

### EventBridge Data Source

Send events to EventBridge:

```python
api.add_data_source(
    "events",
    eventbridge="arn:aws:events:us-east-1:123456789012:event-bus/my-bus"
)
```

### OpenSearch Data Source

Full-text search with OpenSearch:

```python
api.add_data_source(
    "search",
    opensearch="https://search-domain.us-east-1.es.amazonaws.com"
)
```

### RDS Data Source

Connect to Aurora Serverless databases:

```python
api.add_data_source(
    "database",
    rds={
        "cluster_arn": "arn:aws:rds:us-east-1:123456789012:cluster:my-cluster",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:my-secret",
        "database_name": "mydb",  # Optional
    }
)
```

### None Data Source

For local resolvers that don't need a backend:

```python
api.add_data_source("local", none=True)
```

## Resolvers

Resolvers connect GraphQL operations to data sources. Stelvio supports both JavaScript (APPSYNC_JS) and VTL resolvers.

### Unit Resolvers

Unit resolvers use a single data source:

```python
# JavaScript resolver
api.add_resolver(
    "Query getUser",
    data_source="users",
    code='''
        export function request(ctx) {
            return {
                operation: "GetItem",
                key: util.dynamodb.toMapValues({ id: ctx.args.id })
            };
        }
        export function response(ctx) {
            return ctx.result;
        }
    '''
)

# VTL resolver
api.add_resolver(
    "Query getUser",
    data_source="users",
    request_template='''
        {
            "version": "2018-05-29",
            "operation": "GetItem",
            "key": { "id": $util.dynamodb.toDynamoDBJson($ctx.args.id) }
        }
    ''',
    response_template="$util.toJson($ctx.result)"
)
```

### Pipeline Resolvers

Pipeline resolvers chain multiple functions together:

```python
# Create functions
api.add_function(
    "validate-input",
    "local",
    code='''
        export function request(ctx) {
            if (!ctx.args.name) {
                util.error("Name is required");
            }
            return {};
        }
        export function response(ctx) {
            return ctx.args;
        }
    '''
)

api.add_function(
    "create-user",
    "users-table",
    code='''
        export function request(ctx) {
            return {
                operation: "PutItem",
                key: util.dynamodb.toMapValues({ id: util.autoId() }),
                attributeValues: util.dynamodb.toMapValues(ctx.prev.result)
            };
        }
        export function response(ctx) {
            return ctx.result;
        }
    '''
)

# Create pipeline resolver
api.add_resolver(
    "Mutation createUser",
    kind="pipeline",
    functions=["validate-input", "create-user"],
    code='''
        export function request(ctx) {
            return {};
        }
        export function response(ctx) {
            return ctx.prev.result;
        }
    '''
)
```

## Operation Format

The `add_resolver` operation string format is `"Type field"`:

- `"Query getUser"` - Query type, getUser field
- `"Mutation createUser"` - Mutation type, createUser field
- `"Subscription onUserCreated"` - Subscription type
- `"User posts"` - Field resolver on User type

## Linking to Lambda Functions

AppSync APIs can be linked to Lambda functions, giving them permission to execute GraphQL operations:

```python
from stelvio.aws.appsync import AppSync
from stelvio.aws.function import Function

api = AppSync("my-api", "schema.graphql")
api.add_data_source("users", handler="functions/users.handler")
api.add_resolver("Query getUser", data_source="users", code="...")

# Link API to a function
Function(
    "api-client",
    handler="functions/client.handler",
    links=[api]
)
```

The linked function receives these environment variables:

| Variable | Description |
|----------|-------------|
| `STELVIO_LINK_<name>_URL` | GraphQL endpoint URL |
| `STELVIO_LINK_<name>_API_ID` | AppSync API ID |
| `STELVIO_LINK_<name>_ARN` | AppSync API ARN |

And IAM permissions for `appsync:GraphQL` on the API.

## Accessing API Properties

After creating resources, you can access API properties:

```python
api = AppSync("my-api", "schema.graphql")

# These are Pulumi Outputs
url = api.url       # GraphQL endpoint URL
api_id = api.api_id # AppSync API ID  
arn = api.arn       # AppSync API ARN
```

## Customization

You can customize the underlying Pulumi resources:

```python
api = AppSync(
    "my-api",
    "schema.graphql",
    customize={
        "api": {
            "xray_enabled": True,
            "log_config": {
                "cloudwatch_logs_role_arn": "...",
                "field_log_level": "ALL",
            }
        }
    }
)
```

| Key | Resource | Description |
|-----|----------|-------------|
| `api` | `aws.appsync.GraphQLApi` | The GraphQL API |

## Complete Example

```python
from stelvio.aws.appsync import AppSync

# Create API with schema
api = AppSync("users-api", """
    type Query {
        getUser(id: ID!): User
        listUsers: [User]
    }
    
    type Mutation {
        createUser(name: String!, email: String!): User
    }
    
    type User {
        id: ID!
        name: String!
        email: String
    }
""")

# Add data sources
api.add_data_source("users-table", dynamodb="users")
api.add_data_source("local", none=True)

# Add functions for pipeline
api.add_function(
    "validate",
    "local",
    code='''
        export function request(ctx) {
            if (!ctx.args.name) util.error("Name required");
            return {};
        }
        export function response(ctx) { return ctx.args; }
    '''
)

api.add_function(
    "save-user",
    "users-table",
    code='''
        export function request(ctx) {
            return {
                operation: "PutItem",
                key: util.dynamodb.toMapValues({ id: util.autoId() }),
                attributeValues: util.dynamodb.toMapValues(ctx.prev.result)
            };
        }
        export function response(ctx) { return ctx.result; }
    '''
)

# Add resolvers
api.add_resolver(
    "Query getUser",
    data_source="users-table",
    code='''
        export function request(ctx) {
            return {
                operation: "GetItem",
                key: util.dynamodb.toMapValues({ id: ctx.args.id })
            };
        }
        export function response(ctx) { return ctx.result; }
    '''
)

api.add_resolver(
    "Query listUsers",
    data_source="users-table",
    code='''
        export function request(ctx) {
            return { operation: "Scan" };
        }
        export function response(ctx) { return ctx.result.items; }
    '''
)

api.add_resolver(
    "Mutation createUser",
    kind="pipeline",
    functions=["validate", "save-user"],
    code='''
        export function request(ctx) { return {}; }
        export function response(ctx) { return ctx.prev.result; }
    '''
)
```

## AWS Resources Created

For each AppSync API, Stelvio creates:

| Resource | Description |
|----------|-------------|
| **GraphQL API** | The AppSync GraphQL API |
| **API Key** | API key for authentication (if `api_key_expires > 0`) |
| **Data Sources** | Connections to backend services |
| **IAM Roles** | Service roles for data source access |
| **Functions** | Pipeline functions for resolvers |
| **Resolvers** | GraphQL field resolvers |
| **Lambda Functions** | For Lambda data sources and authorizers |
| **Lambda Permissions** | Allow AppSync to invoke Lambda functions |
