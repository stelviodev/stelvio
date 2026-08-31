# Working with WebSocket APIs in Stelvio

This guide explains how to build WebSocket APIs with Stelvio using
[Amazon API Gateway WebSocket APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-websocket-api.html).
You'll learn how to define routes, protect `$connect` with authorizers, put the
API behind a custom domain, and reply to clients with the management API.

`WebsocketApi` creates an API Gateway v2 WebSocket API, an auto-deploy stage
(default `$default`), a CloudWatch log group for access logs, a Lambda function
and integration for each handler, and the IAM permissions API Gateway needs to
invoke those functions. Route selection defaults to `$request.body.action`: a
JSON message `{"action": "ping", ...}` matches a `ping` route.

WebSocket APIs are for server push and two-way traffic: chat, live updates, and
long-running jobs. For request/response HTTP endpoints, use
[`HttpApi`](http-api.md).

!!! important "Return values never reach the client"
    Lambda return values are not sent to the WebSocket client. To reply or
    broadcast, call `PostToConnection` (see
    [Sending Messages to Clients](#sending-messages-to-clients)).

## Creating a WebSocket API

```python
from stelvio.aws.api_gateway import WebsocketApi

api = WebsocketApi("chat")
api.route("$connect", "functions/chat.connect")
api.route("$disconnect", "functions/chat.disconnect")
api.route("$default", "functions/chat.default")
api.route("ping", "functions/chat.ping")
```

That's enough for a working API. Stelvio uses the name for the AWS resource
names and the console label.

The deployed endpoint is available as `api.url` and always uses the `wss://`
scheme. Without a custom domain it includes the stage path, including
`$default` (HTTP APIs omit that stage from the URL):

```python
api.url  # wss://{api-id}.execute-api.{region}.amazonaws.com/$default
```

!!! note "Connection limits"
    API Gateway WebSocket connections last at most 2 hours, idle out after
    10 minutes, accept messages up to 128 KB, and time out integrations after
    29 seconds. See the
    [AWS WebSocket quota table](https://docs.aws.amazon.com/apigateway/latest/developerguide/limits.html#apigateway-execution-service-websocket-limits-table).

### API Configuration

```python
from stelvio.aws.api_gateway import ApiDomain, WebsocketApi

# Basic API with default settings
api = WebsocketApi("chat")

# Named stage instead of $default
api = WebsocketApi("chat", stage_name="production")

# Custom route selection (messages use {"type": "ping", ...})
api = WebsocketApi("chat", route_selection_expression="$request.body.type")

# API with a custom domain owned by this API
api = WebsocketApi("chat", domain_name="chat.example.com")

# API with a shared custom domain
domain = ApiDomain("public-domain", domain_name="api.example.com")
api = WebsocketApi("chat", domain=domain, api_mapping_key="chat")

# Disable the default execute-api hostname (requires a custom domain)
api = WebsocketApi(
    "chat",
    domain_name="chat.example.com",
    disable_execute_api_endpoint=True,
)
```

Available configuration options:

| Option | Default | Description |
|--------|---------|-------------|
| `domain_name` | `None` | Custom domain name for an API-owned domain. Requires a DNS provider. Cannot be combined with `domain`. |
| `domain` | `None` | Shared `ApiDomain` component for mapping multiple APIs to one domain. Cannot be combined with `domain_name`. |
| `api_mapping_key` | `None` | Path segment for a custom domain mapping, such as `chat` or `ws/v1`. Requires `domain_name` or `domain`. |
| `stage_name` | `"$default"` | WebSocket API stage name. Use `"$default"` or a name containing letters, numbers, hyphens, and underscores. The execute-api URL always includes the stage path (including `$default`). |
| `route_selection_expression` | `"$request.body.action"` | Expression evaluated against each message to select a route key. For example, `$request.body.type` routes on a `type` field. |
| `disable_execute_api_endpoint` | `False` | Disable the default `execute-api` hostname. Requires a custom domain. |
| `access_log_retention_days` | `30` | One of CloudWatch's retention values (`1`, `3`, `5`, `7`, `14`, `30`, `60`, `90`, …). Set to `"forever"` to keep logs indefinitely. |

!!! note "One domain option at a time"
    Set either `domain_name` or `domain`, not both. Combining them raises an error.

!!! warning "Add routes before resource creation"
    Add all routes and authorizers before accessing `api.resources`. After
    resources are created, Stelvio rejects further route and authorizer
    changes.

    Reading `api.url`, `api.arn`, `api.api_id`, or `api.execution_arn` does not
    lock the API: you can still add routes afterward. With a custom domain,
    `api.url` is computed from the domain name alone.

## Defining Routes

```python
api.route(route_key, handler)
```

Route keys use API Gateway's native values:

- `$connect`: runs when a client opens a connection. The return value is the
  handshake; a non-2xx status rejects the connection.
- `$disconnect`: runs when the connection closes. AWS does not guarantee it
  fires (the client can disappear or the network can drop).
- `$default`: catches messages that don't match another route
- Custom names such as `ping` or `sendMessage`: selected from
  `$request.body.action`

Each route key must be unique. Adding the same key twice raises an error.

```python
api.route("$connect", "functions/chat.connect")
api.route("$default", "functions/chat.fallback")
api.route("sendMessage", "functions/chat.send")
```

### Connecting Lambda Functions

The handler accepts the same forms as other Stelvio Lambda integrations: a path
string, a `FunctionConfig`, a dictionary, or an existing `Function` instance.

Routes that point to the same handler share a single Lambda function and
integration. Configure a shared function on only one of its routes:

```python
api.route("$connect", "functions/chat.lifecycle", memory=512)
api.route("$disconnect", "functions/chat.lifecycle")
```

You can pass Lambda options directly when the handler is a string:

```python
api.route(
    "sendMessage",
    "functions/chat.send",
    memory=512,
    timeout=20,
)
```

### Lambda Event Format

WebSocket handlers receive the API Gateway WebSocket event. The connection ID
and route key live under `requestContext`:

```python
# functions/chat.py
import json

def connect(event, context):
    connection_id = event["requestContext"]["connectionId"]
    return {"statusCode": 200}  # not sent to the client

def send(event, context):
    route_key = event["requestContext"]["routeKey"]
    body = json.loads(event.get("body") or "{}")
    return {"statusCode": 200}  # not sent to the client
```

A client message like `{"action": "sendMessage", "text": "hi"}` invokes the
`sendMessage` route; the JSON body is available as `event["body"]`.

## Authorization

Authorization runs only during `$connect`. Routes are public by default. Pass
`auth=` on the `$connect` route to require a Lambda authorizer or IAM.

```python
auth = api.add_lambda_authorizer(
    "token-auth",
    "functions/auth.authorize",
    identity_sources=["route.request.querystring.token"],
)
api.route("$connect", "functions/chat.connect", auth=auth)
api.route("$default", "functions/chat.default")
```

Passing `auth=` on any other route key raises an error.

### Lambda Authorizers

WebSocket Lambda authorizers are `REQUEST` authorizers. Identity sources use
WebSocket selection expressions such as `route.request.header.Authorization`
and `route.request.querystring.token`.

The authorizer must return an API Gateway IAM policy response. WebSocket APIs
do not support the HTTP API simple response format:

```python
# functions/auth.py
def authorize(event, context):
    token = (event.get("queryStringParameters") or {}).get("token")
    effect = "Allow" if token == "allow" else "Deny"
    return {
        "principalId": "websocket-user",
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Effect": effect,
                    "Resource": event["methodArn"],
                }
            ],
        },
    }
```

`add_lambda_authorizer` accepts:

- `name`: Unique authorizer name within the API
- `handler`: Lambda function path, config, or `Function` instance
- `identity_sources`: Non-empty list of selection expressions (required)
- `**function_config`: Additional Lambda configuration (memory, timeout, etc.)

### IAM Authorization

For IAM authorization, clients must SigV4-sign the connection request:

```python
api.route("$connect", "functions/chat.connect", auth="IAM")
```

!!! note "No JWT authorizers"
    WebSocket APIs do not support native JWT authorizer resources. Validate JWTs
    in a Lambda authorizer instead. For Cognito, either validate User Pool
    tokens in that authorizer, or obtain AWS credentials from a Cognito
    Identity Pool and use them with `auth="IAM"` and a SigV4-signed connection
    request.

## Custom Domains

For a single API on a single domain, pass `domain_name` directly:

```python
api = WebsocketApi("chat", domain_name="chat.example.com")
```

Custom domains require a DNS provider configured on your Stelvio app. See the
[DNS guide](../../concepts/dns.md). Unless you pass `certificate_arn` on a
shared `ApiDomain`, Stelvio creates the ACM certificate, validates it with DNS,
creates the API Gateway domain, and publishes the DNS record.

Pass a `domain` component when multiple APIs should share one domain. Each API
on the shared domain must use a distinct `api_mapping_key`.

```python
from stelvio.aws.api_gateway import ApiDomain, WebsocketApi

domain = ApiDomain("public-domain", domain_name="api.example.com")

chat_api = WebsocketApi("chat", domain=domain, api_mapping_key="chat")
admin_api = WebsocketApi("admin-ws", domain=domain, api_mapping_key="admin")
```

With a custom domain, `api.url` uses `wss://` and includes the mapping key when
set, for example `wss://api.example.com/chat`.

To use an existing ACM certificate (for example a wildcard already in the
account), pass `certificate_arn` on `ApiDomain`:

```python
domain = ApiDomain(
    "public-domain",
    domain_name="api.example.com",
    certificate_arn="arn:aws:acm:us-east-1:123456789012:certificate/abc123",
)
```

!!! warning "WebSocket and HTTP APIs cannot share a domain"
    A WebSocket API cannot share a custom domain with `HttpApi` or `RestApi`.
    AWS rejects the `ApiMapping`. Use a separate domain for WebSocket APIs.

!!! tip "Disable the default endpoint"
    Set `disable_execute_api_endpoint=True` when all clients should use your
    custom domain. AWS will then reject requests to the default
    `execute-api` hostname. This option requires `domain_name` or `domain`.

!!! note "Shared-domain mapping keys"
    Only one API can use the root mapping for a shared `ApiDomain`. Give every
    additional API on that domain a unique `api_mapping_key`.

    Mapping keys can contain `/` to create nested paths, such as `ws/v1`, but
    cannot start or end with `/`.

## Access Logs

`WebsocketApi` enables access logging by default with a 30-day retention. You can
change the retention or keep logs indefinitely:

```python
# Keep logs for 90 days
api = WebsocketApi("chat", access_log_retention_days=90)

# Keep logs indefinitely
api = WebsocketApi("chat", access_log_retention_days="forever")
```

Logs are written in JSON and include request ID, source IP, request time, route
key, connection ID, event type, status, and any integration error message.

## Linking

Link a `WebsocketApi` to a function when that function needs to call
`PostToConnection` or read the API URL. Linking grants
`execute-api:ManageConnections` and injects the API URL, management URL, and
execution ARN.

A route handler can link to the same API:

```python
from stelvio.aws.api_gateway import WebsocketApi

api = WebsocketApi("chat")
api.route("$connect", "functions/chat.connect")
api.route("ping", "functions/chat.reply", links=[api])
```

For an API named `chat`, the linked function receives these properties:

| `stlv_resources` property | Environment variable | Description |
|---------------------------|----------------------|-------------|
| `Resources.chat.api_url` | `STLV_CHAT_API_URL` | WebSocket URL (`wss://…`), including the mapping key when configured. |
| `Resources.chat.api_management_url` | `STLV_CHAT_API_MANAGEMENT_URL` | Management API URL (`https://{api-id}.execute-api.{region}.amazonaws.com/{stage}`). Always the execute-api hostname, even when the API uses a custom domain. |
| `Resources.chat.api_execution_arn` | `STLV_CHAT_API_EXECUTION_ARN` | API Gateway execution ARN for IAM policies. |

### Link Permissions

Linked functions receive:

- `execute-api:ManageConnections` on `{execution_arn}/*/*/@connections/*`

### Sending Messages to Clients

Use the linked management URL as the `apigatewaymanagementapi` endpoint:

```python
# functions/chat.py
import json

import boto3
from stlv_resources import Resources

def reply(event, context):
    client = boto3.client(
        "apigatewaymanagementapi",
        endpoint_url=Resources.chat.api_management_url,
    )

    connection_id = event["requestContext"]["connectionId"]
    payload = json.dumps({"echo": event.get("body")}).encode("utf-8")
    try:
        client.post_to_connection(ConnectionId=connection_id, Data=payload)
    except client.exceptions.GoneException:
        pass
    return {"statusCode": 200}
```

`post_to_connection` raises `GoneException` when the connection is already
closed.

## Customization

`WebsocketApi` and `ApiDomain` support the `customize` parameter for overriding
properties on the underlying Pulumi resources. For an overview of how
customization works, see the [Customization guide](../../concepts/customization.md).

### Resource Keys

| Component | Resource Key | Pulumi Args Type | Description |
|-----------|--------------|------------------|-------------|
| `WebsocketApi` | `api` | [ApiArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/api/#inputs) | The API Gateway v2 WebSocket API. |
| `WebsocketApi` | `stage` | [StageArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/stage/#inputs) | The auto-deploy stage (default `$default`). |
| `WebsocketApi` | `log_group` | [LogGroupArgs](https://www.pulumi.com/registry/packages/aws/api-docs/cloudwatch/loggroup/#inputs) | The CloudWatch access log group. |
| `WebsocketApi` | `api_mapping` | [ApiMappingArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/apimapping/#inputs) | The custom domain mapping when `domain_name` or `domain` is set. |
| `ApiDomain` | `certificate` | [CertificateArgs](https://www.pulumi.com/registry/packages/aws/api-docs/acm/certificate/#inputs) | The ACM certificate for the custom domain. |
| `ApiDomain` | `domain` | [DomainNameArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/domainname/#inputs) | The API Gateway v2 custom domain. |
| `ApiDomain` | `dns_record` | DNS provider record args | The DNS record pointing the custom domain to API Gateway. |

## Next Steps

- [Working with HTTP APIs](http-api.md) - Request/response HTTP endpoints on API Gateway v2.
- [Working with Lambda Functions](lambda.md) - Learn how Lambda packaging and configuration work.
- [Authentication with Cognito](cognito.md) - User pools and identity pools for custom or IAM auth.
- [Linking](../../concepts/linking.md) - Learn how links generate environment variables and permissions.
- [DNS](../../concepts/dns.md) - Configure a DNS provider for custom domains.
