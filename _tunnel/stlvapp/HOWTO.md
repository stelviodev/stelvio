# Stelvio Tunnel: Live Lambda Development

## Overview

`stlv dev` enables developers to execute Lambda function code locally while maintaining full access to AWS infrastructure. When a Lambda function is invoked through AWS API Gateway, the request is tunneled to your local development machine, executed there, and the response is sent back through AWS—providing rapid iteration without repeated deployments.

**Key Benefits:**
- Instant code changes without redeployment
- Full AWS infrastructure access (DynamoDB, S3, etc.)
- Realistic request/response flow through actual API Gateway
- Maintains production-like AWS URLs

## Problem Statement

Traditional serverless development requires a full deployment cycle for every code change, which slows iteration. Local testing frameworks like SAM or LocalStack simulate AWS services but don't provide access to actual deployed resources. `stlv dev` bridges this gap by keeping infrastructure in AWS while running code locally.

## Solution Architecture

The tunnel system consists of four main components:

1. **Replacement Lambda**: Deployed to AWS in place of user functions during dev mode
2. **Infrastructure Lambda**: HTTP-to-MQTT bridge that handles request forwarding
3. **MQTT Message Broker**: AWS IoT Core providing bidirectional pub/sub messaging
4. **Local WebSocket Client**: Subscribes to MQTT, executes handlers, publishes responses

```
AWS API Gateway → Replacement Lambda → Infrastructure Lambda → MQTT Broker
                                                                    ↓
                                                            Local WebSocket Client
                                                                    ↓
                                                            Execute Handler Locally
                                                                    ↓
                                                            Response via MQTT
```

## Request Flow

### Step 1: Incoming Request
An HTTP request hits API Gateway and routes to the Replacement Lambda (deployed during `stlv dev`).

### Step 2: Forward to Infrastructure
The Replacement Lambda (`replacement.py`) packages the complete Lambda event and context, then POSTs to the Infrastructure Lambda endpoint with:
- Original event data
- Lambda context metadata
- Channel ID (dev session identifier)
- Endpoint ID (unique per function)

### Step 3: Publish to MQTT
The Infrastructure Lambda (`incoming.py`):
- Generates a unique Request ID for this invocation
- Wraps the payload with type `request-received`
- Publishes to MQTT topic `public/{channel_id}`
- Opens a WebSocket connection to the same topic
- Waits up to 30 seconds for a response with matching Request ID

### Step 4: Local Execution
The local WebSocket client (`ws.py`) running on the developer's machine:
- Receives the message from MQTT
- Routes to the correct handler using Endpoint ID
- Loads the actual handler module from local filesystem
- Reconstructs the Lambda event and context
- Executes the handler function locally
- Captures the response payload

### Step 5: Response Path
The local client:
- Wraps the response with type `request-processed` and matching Request ID
- Publishes back to MQTT topic `public/{channel_id}`
- Infrastructure Lambda receives it, validates Request ID, and returns to Replacement Lambda
- Replacement Lambda returns the response to API Gateway
- Client receives the response as if Lambda ran in AWS

## Component Details

### Replacement Lambda (`replacement.py`)
Deployed in AWS when `context().tunnel_mode` is enabled. Contains minimal code to forward requests:
- Receives Lambda invocation from API Gateway
- Extracts event and context
- POSTs to Infrastructure Lambda with Channel ID and Endpoint ID
- Returns Infrastructure Lambda's response

Created by `_create_lambda_tunnel_archive()` with injected Channel ID and Endpoint ID placeholders.

### Infrastructure Lambda (`incoming.py`)
Acts as the HTTP-to-MQTT bridge:
- Exposes HTTP endpoint via API Gateway: `/tunnel/{channel_id}`
- Accepts POST requests from Replacement Lambdas
- Generates unique Request ID per invocation
- Publishes to MQTT with wrapped payload
- Opens temporary WebSocket subscription to wait for response
- Returns response payload to caller
- 30-second timeout for response

### MQTT Message Broker (AWS IoT Core)
Provides pub/sub messaging using topic `public/{channel_id}`:
- Replacement → Infrastructure → MQTT (publish)
- MQTT → Local Client (subscription)
- Local Client → MQTT (publish response)
- MQTT → Infrastructure (subscription for response)

Uses Cognito Identity Pool for unauthenticated access with SigV4 signing.

### Local WebSocket Client (`ws.py`)
The `stlv dev` command establishes a persistent connection:
- Connects to AWS IoT Core endpoint using WebSocket over MQTT
- Subscribes to `public/{channel_id}` topic
- Maintains an event loop for async message handling
- Routes messages through registered `TunnelableComponent` handlers
- Each handler checks Endpoint ID to claim ownership
- Logs request details (method, path, status, duration)

### TunnelableComponent
Base class enabling tunnel support for any Stelvio component:
- `_dev_endpoint_id`: Unique identifier per component instance
- `handle_tunnel_event()`: Routes messages by Endpoint ID
- `_handle_tunnel_event()`: Abstract method implemented by subclasses

For `Function` components:
- Dynamically loads handler module from local filesystem
- Reconstructs `LambdaContext` object
- Invokes handler with original event and context
- Returns response payload

## Implementation Details

### Tunnel Mode Detection
Components check `context().tunnel_mode` during resource creation:
```python
if context().tunnel_mode and not self.config.is_tunnel_infrastructure:
    # Deploy replacement lambda
    endpoint_id = uuid.uuid4().hex
    self._dev_endpoint_id = endpoint_id
    WebsocketHandlers.register(self.handle_tunnel_event)
    # Create lambda with replacement.py code
else:
    # Normal deployment with actual handler
```

### Channel ID and Endpoint ID System
- **Channel ID**: Identifies a dev session (currently hardcoded to `dev-test`, should be generated per `stlv dev` invocation)
- **Endpoint ID**: Unique UUID per `TunnelableComponent` instance, injected into Replacement Lambda code
- Message routing: All functions share the MQTT channel, but filter by Endpoint ID to claim their messages

### Request ID for Concurrency
Each invocation generates a unique Request ID to handle concurrent requests:
- Prevents response mix-up when same function invoked multiple times simultaneously
- Infrastructure Lambda creates Request ID before publishing to MQTT
- Local client includes Request ID in response
- Infrastructure Lambda matches Request ID before returning response

### Handler Registration
When `stlv dev` runs:
1. Deployment occurs with `tunnel_mode=True`
2. Each `TunnelableComponent` registers with `WebsocketHandlers.register()`
3. WebSocket client invokes all registered handlers for each message
4. Each handler checks Endpoint ID to determine ownership
5. Matching handler processes message and responds

### Tunnel Infrastructure Setup
The `create_tunnel_infrastructure()` function deploys:
- AWS IoT Core configuration for MQTT
- Cognito Identity Pool for unauthenticated access
- Infrastructure Lambda with IoT publish permissions
- API Gateway endpoint for Infrastructure Lambda
- IAM policies for pub/sub on `public/*` topics

Currently implemented in `stlv_app.py`, should be moved to Stelvio core modules.

## Current Limitations & Open Questions

### Module Loading and Virtual Environments
Local handler execution imports modules from the project directory but may not respect Lambda's `requirements.txt`. The local environment must have all dependencies installed that the Lambda would use in AWS.

**Consideration**: Isolate handler execution in a virtual environment matching Lambda's Python runtime and dependencies.

### Message Size Limits
AWS IoT Core has a 128 KB message size limit. Large Lambda events or responses may exceed this.

**Mitigation**: Consider S3-based payload storage for large messages, similar to SQS extended client pattern.

### Non-Lambda Component Tunneling
Current implementation focuses on Lambda functions. Other AWS resources could benefit from tunneling:
- **S3 Bucket**: Tunnel GET/PUT operations to local filesystem
- **Static Website**: Could proxy to a locally executed `npm dev` server

Each would need custom `TunnelableComponent` implementations.

### Performance Considerations
Added latency from tunneling:
- Replacement Lambda → Infrastructure Lambda: ~50-100ms
- MQTT publish/subscribe: ~50-100ms
- Local execution + network: varies
- Total overhead: ~200-400ms per request
- Linking? Do I actually have IAM permissions in locally executed lambdas?
- 

### Cleanup and Error Handling
- No automatic cleanup of Infrastructure Lambda WebSocket connections on timeout
- `time.sleep(1)` in `handle_tunnel_event()` should be removed
- Better error propagation from local handlers to caller
- Graceful handling of local handler crashes

### Security
Current Cognito setup allows unauthenticated access to `public/*` topics. For production tunnel services, implement:
- Authentication for tunnel infrastructure endpoints
- Scoped credentials per developer/session
- Audit logging of tunnel usage
