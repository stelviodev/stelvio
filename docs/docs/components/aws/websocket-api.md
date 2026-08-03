# Working with WebSocket APIs in Stelvio

`WebsocketApi` creates an API Gateway v2 WebSocket API with Lambda proxy routes.

```python
from stelvio.aws.api_gateway import WebsocketApi

api = WebsocketApi("chat")
api.route("$connect", "handlers/connect.main")
```

Route keys use API Gateway's native values, including `$connect`, `$disconnect`,
`$default`, and custom action names. Add routes before accessing `api.resources`.

The auto-deploy stage is always `$default` (not configurable). The deployed
endpoint is available as `api.url` and uses the `wss://` scheme:

```python
from stelvio import export_output

export_output("chat_url", api.url)
```

Message routes (`$default` and custom actions) get a `$default` route response so
Lambda return values can be sent back to the client. `$connect` and `$disconnect`
do not.

Linking a function to the API (`links=[api]`) grants
`execute-api:ManageConnections` so the Lambda can call `PostToConnection`.

## Custom Domains

For an API-owned domain, pass `domain_name`:

```python
api = WebsocketApi("chat", domain_name="chat.example.com")
```

To share an `ApiDomain`, use a distinct `api_mapping_key` for each API:

```python
from stelvio.aws.api_gateway import ApiDomain

domain = ApiDomain("public", domain_name="api.example.com")
api = WebsocketApi("chat", domain=domain, api_mapping_key="chat")
```

With a custom domain, `api.url` and the linked `api_url` property use
`wss://` and include the mapping key, such as
`wss://api.example.com/chat`. Without one, the execute-api `$default` URL is
unchanged. Custom domains require a configured DNS provider.

Set `disable_execute_api_endpoint=True` when clients should use only the custom
domain; AWS then rejects requests to the default `execute-api` hostname. This
option requires `domain_name` or `domain`.

## Connection Authorization

Authorization runs only during `$connect`. A Lambda `REQUEST` authorizer can use
WebSocket identity sources such as `route.request.header.Authorization` and
`route.request.querystring.token`:

```python
auth = api.add_lambda_authorizer(
	"token-auth",
	"handlers/auth.authorize",
	identity_sources=["route.request.querystring.token"],
)
api.route("$connect", "handlers/connect.main", auth=auth)
```

The authorizer must return an API Gateway IAM policy response containing
`principalId` and a `policyDocument` with `Version`, `Statement`, `Action`,
`Effect`, and `Resource`. Authorization is not supported on other route keys.

For IAM authorization, clients must SigV4-sign the connection request:

```python
api.route("$connect", "handlers/connect.main", auth="IAM")
```

WebSocket APIs do not support native JWT authorizer resources. Validate JWTs in
the Lambda authorizer instead. For Cognito, either validate User Pool tokens in
that authorizer, or obtain AWS credentials from a Cognito Identity Pool and use
them with `auth="IAM"` and a SigV4-signed connection request.

## Customization

`WebsocketApi` supports `customize` for the underlying Pulumi resources. See the
[Customization guide](../../concepts/customization.md).

| Resource Key | Pulumi Args Type | Description |
|--------------|------------------|-------------|
| `api` | [ApiArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/api/#inputs) | The WebSocket API. |
| `stage` | [StageArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/stage/#inputs) | The `$default` auto-deploy stage. |
| `api_mapping` | [ApiMappingArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/apimapping/#inputs) | The custom domain mapping when a domain is configured. |
