# Working with API Gateway in Stelvio

This guide explains how to create and manage API endpoints with Stelvio. You'll learn 
how to define routes, connect them to Lambda functions, and understand the different
organizational patterns available to you.

## Creating an API

Creating an API Gateway in Stelvio is straightforward. You start by defining your API
instance:

```python
from stelvio.aws.apigateway import Api

api = Api('my-api')
```

The name you provide will be used as part of your API's URL and for identifying it in
the AWS console.

### API Configuration

For production use cases, you can configure your API Gateway with additional settings:

```python
from stelvio.aws.apigateway import Api

# Basic API with default settings
api = Api('my-api')

# API with custom domain
api = Api('my-api', domain_name='api.example.com')

# API with custom stage name
api = Api('my-api', stage_name='production')

# API with edge-optimized endpoint
api = Api('my-api', endpoint_type='edge')

# API with all custom settings
api = Api(
    'my-api',
    domain_name='api.example.com',
    stage_name='production', 
    endpoint_type='edge'
)
```

Available configuration options:

- **`domain_name`** (optional): Custom domain name for your API. See [Custom Domains](#custom-domains) section below.
- **`stage_name`** (optional): Stage name for your API deployment. Defaults to `"v1"`.
- **`endpoint_type`** (optional): API Gateway endpoint type - `"regional"` (default) or `"edge"`. See [Endpoint Types](#endpoint-types) below.

#### Endpoint Types

Choose the right endpoint type based on your use case:

- **`"regional"`**: Best for applications primarily serving users in a specific AWS region. Lower latency for regional users and simpler configuration.
- **`"edge"`**: Best for applications serving global users. Uses CloudFront to cache responses at edge locations worldwide for better global performance.

```python
# Regional endpoint (default)
api = Api('my-api', endpoint_type='regional')

# Edge-optimized endpoint  
api = Api('my-api', endpoint_type='edge')
```

#### Stage Names

Stage names help organize different versions or environments of your API:

```python
# Development stage
api = Api('my-api', stage_name='dev')

# Production stage  
api = Api('my-api', stage_name='production')

# Version-based staging
api = Api('my-api', stage_name='v2')
```

The stage name becomes part of your API URL: `https://api-id.execute-api.region.amazonaws.com/{stage_name}/`

## Defining Routes

Stelvio provides a clean, intuitive way to define API routes. The basic pattern is:

```python
api.route(http_method, path, handler)
```

Let's look at each component:

- `http_method`: The HTTP verb for this route ('GET', 'POST', etc.)
- `path`: The URL path for this endpoint ('/users', '/orders/{id}', etc.)
- `handler`:  Lambda function handler or path to it

Here's a complete example:

```python
from stelvio.aws.apigateway import Api

api = Api('my-api')

# Basic route
api.route('GET', '/users', 'functions/users.index')

# Route with path parameter
api.route('GET', '/users/{id}', 'functions/users.get')

# Route with different HTTP method
api.route('POST', '/users', 'functions/users.create')

# Deployment happens automatically when routes or configurations change.
```

### HTTP Methods

Stelvio supports all standard HTTP methods. You can specify them in several ways:

```python

from stelvio.aws.apigateway import Api

api = Api('my-api')

# Single method (case insensitive)
api.route('GET', '/users', 'functions/users.index')
api.route('get', '/users', 'functions/users.index')

# Multiple methods for one endpoint
api.route(['GET', 'POST'], '/users', 'functions/users.handler')

# Any HTTP method
api.route('ANY', '/users', 'functions/users.handler')
api.route('*', '/users', 'functions/users.handler')  # Alternative syntax
```

## Lambda function Integration

Stelvio offers flexible ways to connect your routes to Lambda functions. The handler 
path in your route definition can have two formats:

1. For [Single-File Functions](lambda.md#single-file-lambda-functions) use a simple path
   convention:

    ```
    folder/file.function_name
    ```

2. [Folder-Based Functions](lambda.md#folder-based-lambda-functions) (when you need to 
   package multiple files) use this format:

    ```
    folder/path::file.function_name
    ```
    Where everything before `::` is the path to the folder of your lambda function, and 
    everything after is the relative path to file and function name within that folder.

    Examples:
    ```python
    # Single-file function
    api.route('GET', '/users', 'functions/users.index')
    
    # Folder-based function
    api.route('GET', '/orders', 'functions/orders::handler.process_order')
    ```

Stelvio will create lambda automatically from your source file.

When multiple routes point to the same Lambda Function (whether it's a single file or 
folder-based function), Stelvio automatically generates and includes routing code in the 
Lambda package. This routing code ensures each route calls the correct Python function 
as defined in your routes.

```python
# These routes share one Lambda function
# Stelvio will generate routing code to call correct function based on the route
api.route('GET', '/users', 'functions/users.index')
api.route('POST', '/users', 'functions/users.create_user')

# This route uses a different Lambda function
api.route('GET', '/orders', 'functions/orders.index')
```

### Lambda Configuration

The above samples will create functions with default configuration. If you want to
customize Lambda function settings like memory size, timeout or
runtime settings, you have several options:

1. Through `FunctionConfig` class
    
    ```python
    # In this example we configure custom memory size and timeout
    api.route(
        "GET",
        "/users",
        FunctionConfig(
            handler="functions/users.index",
            memory=512,
            timeout=30,
        ),
    )
    ```

2. Through dictionary `FunctionConfigDict`.
    
    `FunctionConfigDict()` is typed dict so all your keys and values will be typed checked 
    if you use IDE or mypy or other type checking tool.

    ```python
    # In this example we configure custom memory size and timeout
    api.route(
        "GET",
        "/users",
        {
            "handler": "functions/users.index",
            "memory":512,
            "timeout":30,
        },
    )
    ```
   
3. Through keyword arguments
    ```python
    # In this example we configure custom memory size and timeout
    api.route(
        "GET",
        "/users",
        "functions/users.index",
        memory=512,
        timeout=30,
    )
    ```

4. Passing function instance as a handler:

    You can create lambda function yourself and pass it to the route as a handler.

    ```python
    # Defined in separate variable.
    users_fn = Function(
        "users-function",
        handler="functions/users.index",
        memory=512,
    )
    
    api.route("GET", "/users", users_fn)
    
    # Inline.  
    api.route(
        "GET",
        "/orders",
        Function(
            "orders-function",
            folder="functions/orders",
            handler="handler.index",
        ),
    )
    ```
    !!! warning
        When you create function yourself Stelvio will not generate any routing code for
        you, you're responsible for it. 
    
    !!! note "Remember"
        Each instance of `Function` creates new lambda function so if you want to 
        use one function as a handler for multiple routes
        you need to store it in a variable first. 
    

!!! warning "Only One Configuration per Function"
    When multiple routes use same function (identified 
    by the same file for [Single-File Functions](lambda.md#single-file-lambda-functions) 
    and by the same folder (`src`) for 
    [Folder-Based Functions](lambda.md#folder-based-lambda-functions)), the function 
    should be configured only once. If other route uses same function it shares config 
    from the route that has config. 

    If you provide configuration in multiple places for the same function , Stelvio will 
    fail with an error message. This ensures clear and predictable behavior. 
    
    To configure a shared function, either configure it on its first use or create a 
    separate  `Function` instance and reuse it across routes. (As shown above in point 4.)

??? note "A note about handler format for Folder-based functions"
    The `::` format (`folder/path::file.function_name`) for folder-based functions is a 
    convenient shorthand specific to API Gateway routes. However, you can still create 
    folder-based functions using configuration options. Here are all the ways to define 
    a folder-based function:

    ```python
    # Using FunctionConfig class
    api.route(
        "POST",
        "/orders",
        FunctionConfig(
            folder="functions/orders",
            handler="function.handler",
        ),
    )
    
    # Using configuration dictionary
    api.route(
        "POST",
        "/orders",
        {
            "src": 'functions/orders',
            "handler": "function.handler",
        },
    )
    
    # Using keyword arguments
    api.route(
        "POST",
        "/orders",
        folder="functions/orders",
        handler="function.handler",
    )
    
    # Using Function instance
    api.route(
        "GET",
        "/orders",
        Function(
            "orders-function",
            folder="functions/orders",
            handler="handler.index",
        ),
    )
    ```

## Authorization

Stelvio supports AWS API Gateway authorizers to secure your API endpoints. You can use Lambda-based authorizers (Token and Request types), Cognito User Pools, or AWS IAM authorization.

Learn more: [AWS API Gateway Authorizers](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-use-lambda-authorizer.html)

### Token Authorizers (JWT, OAuth)

Token authorizers validate bearer tokens (like JWTs) from a single source, typically the `Authorization` header. They're ideal for OAuth 2.0 or JWT-based authentication.

```python
from stelvio.aws.apigateway import Api

api = Api('my-api')

# Add TOKEN authorizer
jwt_auth = api.add_token_authorizer(
    'jwt-auth',
    'functions/authorizers/jwt.handler',
    identity_source='method.request.header.Authorization',  # default
    ttl=600,  # cache duration in seconds
)

# Use authorizer on routes
api.route('GET', '/protected', 'functions/api/protected.handler', auth=jwt_auth)
```

Your authorizer Lambda function receives the token and returns an IAM policy:

```python
# functions/authorizers/jwt.py
import json

def handler(event, context):
    token = event['authorizationToken']

    # Validate token (e.g., verify JWT signature)
    if is_valid_token(token):
        return {
            'principalId': 'user-id',
            'policyDocument': {
                'Version': '2012-10-17',
                'Statement': [{
                    'Action': 'execute-api:Invoke',
                    'Effect': 'Allow',
                    'Resource': event['methodArn']
                }]
            }
        }

    raise Exception('Unauthorized')
```

**Configuration options:**

- `name`: Unique authorizer name within the API
- `handler`: Lambda function path or Function instance
- `identity_source`: Header to extract token from (default: `"method.request.header.Authorization"`)
- `ttl`: Cache TTL in seconds (default: 300)
- `**function_config`: Additional Lambda configuration (memory, timeout, etc.)

Learn more: [Lambda Token authorizers](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-use-lambda-authorizer.html#api-gateway-lambda-authorizer-token-lambda-function-create)

### Request Authorizers (Multi-Source)

Request authorizers can validate using multiple sources (headers, query strings, context) and have access to the full request. They're useful for complex authentication schemes.

```python
api = Api('my-api')

# Add REQUEST authorizer with multiple identity sources
request_auth = api.add_request_authorizer(
    'custom-auth',
    'functions/authorizers/custom.handler',
    identity_source=[
        'method.request.header.X-API-Key',
        'method.request.querystring.token',
        'method.request.header.X-Session-ID',
    ],
    ttl=300,
)

api.route('POST', '/orders', 'functions/api/orders.handler', auth=request_auth)
```

Your authorizer Lambda receives the full request context:

```python
# functions/authorizers/custom.py
def handler(event, context):
    # Access headers, query params, etc.
    api_key = event['headers'].get('X-API-Key')
    token = event['queryStringParameters'].get('token')
    session_id = event['headers'].get('X-Session-ID')

    # Validate using multiple factors
    if validate_multi_factor(api_key, token, session_id):
        return {
            'principalId': 'user-id',
            'policyDocument': {
                'Version': '2012-10-17',
                'Statement': [{
                    'Action': 'execute-api:Invoke',
                    'Effect': 'Allow',
                    'Resource': event['methodArn']
                }]
            },
            'context': {
                'userId': 'user-123',
                'role': 'admin',
            }
        }

    raise Exception('Unauthorized')
```

**Configuration options:**

- `name`: Unique authorizer name within the API
- `handler`: Lambda function path or Function instance
- `identity_source`: Single source string or list of sources (default: `"method.request.header.Authorization"`)
- `ttl`: Cache TTL in seconds (default: 300)
- `**function_config`: Additional Lambda configuration

Learn more: [Lambda Request authorizers](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-use-lambda-authorizer.html#api-gateway-lambda-authorizer-request-lambda-function-create)

### Cognito User Pool Authorizers

Cognito authorizers integrate with AWS Cognito User Pools for managed authentication. No Lambda function needed.

```python
api = Api('my-api')

# Add Cognito authorizer
cognito_auth = api.add_cognito_authorizer(
    'cognito-auth',
    user_pools=['arn:aws:cognito-idp:us-east-1:123456789:userpool/us-east-1_ABC123'],
    ttl=300,
)

api.route('GET', '/profile', 'functions/api/profile.handler', auth=cognito_auth)
```

Clients must include a valid Cognito JWT token in the `Authorization` header.

**Configuration options:**

- `name`: Unique authorizer name within the API
- `user_pools`: List of Cognito User Pool ARNs
- `ttl`: Cache TTL in seconds (default: 300)

Learn more: [Cognito User Pool authorizers](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-integrate-with-cognito.html)

### IAM Authorization

Use IAM authorization for service-to-service communication or when calling from AWS services with IAM roles.

```python
api = Api('my-api')

# Route with IAM authorization
api.route('POST', '/internal', 'functions/api/internal.handler', auth='IAM')
```

Clients must sign requests using AWS Signature Version 4 (SigV4).

Learn more: [IAM authorization](https://docs.aws.amazon.com/apigateway/latest/developerguide/permissions.html)

### Default Authorization

Set a default authorizer for all routes in an API:

```python
api = Api('my-api')

jwt_auth = api.add_token_authorizer('jwt-auth', 'functions/authorizers/jwt.handler')

# Set default auth - applies to all routes without explicit auth
api.set_default_auth(jwt_auth)

# Uses default auth (jwt_auth)
api.route('GET', '/users', 'functions/api/users.handler')

# Override default with different auth
request_auth = api.add_request_authorizer('custom', 'functions/authorizers/custom.handler')
api.route('POST', '/admin', 'functions/api/admin.handler', auth=request_auth)

# Opt out of default auth
api.route('GET', '/public', 'functions/api/public.handler', auth=False)

# IAM auth (overrides default)
api.route('POST', '/internal', 'functions/api/internal.handler', auth='IAM')
```

### Public Routes

Routes without authentication require `auth=False`:

```python
api = Api('my-api')

# Public route - no authentication
api.route('GET', '/health', 'functions/api/health.handler', auth=False)

# With default auth set, explicitly opt out
api.set_default_auth(jwt_auth)
api.route('GET', '/public', 'functions/api/public.handler', auth=False)
```

### Route-Level Authorization

Each route can specify its own authorization:

```python
api = Api('my-api')

# Create authorizers
jwt_auth = api.add_token_authorizer('jwt-auth', 'functions/authorizers/jwt.handler')
admin_auth = api.add_request_authorizer('admin-auth', 'functions/authorizers/admin.handler')

# Different auth per route
api.route('GET', '/users', 'functions/api/users.handler', auth=jwt_auth)
api.route('POST', '/admin', 'functions/api/admin.handler', auth=admin_auth)
api.route('POST', '/internal', 'functions/api/internal.handler', auth='IAM')
api.route('GET', '/public', 'functions/api/public.handler', auth=False)
```

## CORS

TBD

## Custom Domains

Connecting a custom domain to your API Gateway is essential for production applications. Stelvio simplifies this process by allowing you to specify a custom domain name when creating your API.

To set up a custom domain, you need to provide the `domain_name` parameter when creating your API instance:

```python
from stelvio.aws.apigateway import Api
api = Api('my-api', domain_name='api.example.com')
```

As outlined in the [DNS guide](dns.md), this app configuration will assume you have set up a DNS provider for your app like so:

```python
from stelvio import StelvioApp
from stelvio.cloudflare.dns import CloudflareDns
from stelvio.aws.dns import Route53Dns

app = StelvioApp(
    "my-app",
    dns=Route53Dns("your-route53-zone-id"),  # use Route53 on AWS,
    # dns=CloudflareDns("your-cloudflare-zone-id")  # use Cloudflare as DNS provider,
    # other configurations...
)
```

Behind the scenes, Stelvio will take care of the following high level tasks:

- Make sure the API Gateway responds to requests made to `api.example.com`
- Create a TLS certificate for `api.example.com`
- Create a DNS record that resolves `api.example.com` to the API Gateway endpoint

### Custom Domains in Environments

Obviously, one domain can only be attached to one ApiGateway. If you want to use the same custom domain in multiple environments, you need to assign different subdomains for each environment. 

One way of doing this is to use the environment name as a subdomain. For example, if your custom domain is `api.example.com`, you can use `dev.api.example.com` for the development environment and `prod.api.example.com` for the production environment.

You can achieve this by using the `context().env` variable in your API definition:

```python
@app.run
def run() -> None:
    # With custom domain
    api = Api("todo-api", domain_name=CUSTOM_DOMAIN_NAME if context().env == "prod" else f"{context().env}.{CUSTOM_DOMAIN_NAME}")
    api.route("GET", "/a", handler="functions/todos.get")
```

This way, the API Gateway will respond to requests made to `dev.api.example.com` in the development environment and `prod.api.example.com` in the production environment.


### Behind the Scenes

When you set a custom domain, Stelvio will automatically create the following resources:

- `AcmValidatedDomain`: Stelvio component with the following Pulumi resources:
  - `certificate`: `pulumi_aws.acm.Certificate`
  - `validation_record`: `stelvio.dns.Record`
  - `cert_validation`: `pulumi_aws.acm.CertificateValidation`
- `pulumi_aws.apigateway.DomainName`: Represents the custom domain in API Gateway.
- `stelvio.dns.Record`: A DNS record that points your custom domain to the API Gateway endpoint.
- `pulumi_aws.apigateway.BasePathMapping`: Maps the custom domain to your API Gateway stage.

## Next Steps

Now that you understand API Gateway basics, you might want to explore:

- [Working with Lambda Functions](lambda.md) - Learn more about how to work with Lambda functions
- [Working with DynamoDB](dynamo-db.md) - Learn how to create DynamoDB tables
- [Linking](linking.md) - Learn how linking automates IAM, permissions, envars and more
- [Project Structure](project-structure.md) - Discover patterns for organizing your Stelvio applications
