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

## Advanced Features

### Authorization

TBD

### CORS

TBD

### Custom Domains

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

dns = CloudflareDns(
    zone_id="your-cloudflare-zone-id"
)

app = StelvioApp(
    "my-app",
    dns=dns,
    # other configurations...
)
```

Behind the sceenes, Stelvio will take care of the following high level tasks:

- Make sure the API Gateway responds to requests made to `api.example.com`
- Create a TLS certificate for `api.example.com`
- Create a DNS record that resolves `api.example.com` to the API Gateway endpoint

#### Custom Domains in Environments

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


#### Behind the Scenes

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
