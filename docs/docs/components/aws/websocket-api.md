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

This prototype supports Lambda proxy routes only. It does not include authorizers,
custom domains, access logs, or connection-management helpers.

## Customization

`WebsocketApi` supports `customize` for the underlying Pulumi resources. See the
[Customization guide](../../concepts/customization.md).

| Resource Key | Pulumi Args Type | Description |
|--------------|------------------|-------------|
| `api` | [ApiArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/api/#inputs) | The WebSocket API. |
| `stage` | [StageArgs](https://www.pulumi.com/registry/packages/aws/api-docs/apigatewayv2/stage/#inputs) | The `$default` auto-deploy stage. |
