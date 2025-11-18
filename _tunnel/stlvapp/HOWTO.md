# Stelvio Tunnel (Live Lambda)

## Abstract

**Objective**: User wants to execute code locally, but within the AWS infra

**Solution**:

WHEN `dev` mode is requested THEN a replacement lambda is deployed
WHEN the replacement lambda is hit THEN it should pack the request, send it to a processing endpoint
WHEN the processing endpoint is hit THEN it should publish the request to a message queue and wait for a message containing a result
WHEN `stlv dev` is run THEN a websocket client should listen to `request-received` messages AND post the result by invoking the real lambda locally

## In Depth

### Infra

To create the infrastructure on AWS for the message queue, there is a `create_tunnel_infrastructure` function. This is in `stlv_app.py` for now, but should be moved to stelvio core modules.

CLI got a new command: `dev`

When dev is called, every component that is a `TunnelableComponent` (subclass of `Component`) gets assigned an endpoint id and is registered. Then, a new Websocket client is created (`ws.py`).

Each TunnelableComponent is responsible for its own replacement deployment (`if context().tunnel_mode`).
Each TunnelableComponent implements a `_handle_tunnel_event` function that is called on each message that the replacement lambda generates. In case of a `Function`, it regenerates the request, loads the lambda module locally, executes it, then puts the message on the bus (`await websocket_client.send_json(response_message)`).

For te tunnel infra, we deploy:
- an API endpoint that accepts POST requests
- an mqtt message queue

When the user based lambda is hit, the replacement lambda gets executed.
It then takes the requests, and `POST`s it to the infrastructure lambda.
The infrastructure lambda puts the request to the mqtt bus and waits for the approriate response on the mqtt bus.

Connecting the right lambda with the right request handler s done in `TunnelableComponent.handle_tunnel_event`. The `handle_tunnel_event` checks for the right endpoint id (to ensure the right lambda is called).
Each response is additionally assigned a request id to handle concurrency situations.

### Open Questions

- Module loading / virtual env (requirements.txt should be separate from project env)
- get rid of time.sleeps
- replacement of components other than lambdas:
    - e.g. s3 bucket: would get a lambda-like url
- message size limits
