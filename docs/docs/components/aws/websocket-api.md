# Working with WebSocket APIs in Stelvio

`WebsocketApi` creates an API Gateway v2 WebSocket API with Lambda proxy routes.

```python
from stelvio.aws.api_gateway import WebsocketApi

api = WebsocketApi("chat")
api.route("$connect", "handlers/connect.main")
```

Route keys use API Gateway's native values, including `$connect`, `$disconnect`,
`$default`, and custom action names. Add routes before accessing `api.resources`.

The deployed endpoint is available as `api.url` and uses the `wss://` scheme:

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

This component supports Lambda proxy routes only. It does not include
authorizers, access logs, or connection-management helpers.

## Customization

`WebsocketApi` supports `customize` for the underlying Pulumi resources. See the
[Customization guide](../../concepts/customization.md).

| Resource Key | Pulumi Args Type | Description |
|--------------|------------------|-------------|
| `api` | [ApiArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/api/#inputs) | The WebSocket API. |
| `stage` | [StageArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/stage/#inputs) | The `$default` auto-deploy stage. |
| `api_mapping` | [ApiMappingArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/apimapping/#inputs) | The custom domain mapping when a domain is configured. |
