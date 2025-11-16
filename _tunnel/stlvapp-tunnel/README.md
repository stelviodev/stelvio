# AWS IoT Core Public WebSocket Broadcast

This project demonstrates a public WebSocket broadcast system using AWS IoT Core, Cognito Identity Pool, and Pulumi for infrastructure management.

## Architecture

The system consists of:

1. **AWS IoT Core** - MQTT message broker with WebSocket support
2. **Cognito Identity Pool** - Provides unauthenticated (public) AWS credentials
3. **IAM Policies** - Allows public access to topics under `public/*` with client IDs starting with `public-`
4. **Python Client** - WebSocket client using AWS IoT Device SDK v2

## Infrastructure

The infrastructure is defined in `stlv_app.py` using Pulumi and includes:

- Cognito Identity Pool configured for unauthenticated access
- IAM role for unauthenticated identities with IoT Core permissions
- Automatic IoT Core endpoint discovery

### Deployed Resources

```bash
# View current deployment outputs
cd /workspaces/stelvio/_tunnel/stlvapp-tunnel && uv run stlv outputs
```

Key outputs:
- `iotWssUrl`: WebSocket endpoint (wss://...)
- `identityPoolId`: Cognito Identity Pool ID
- `region`: AWS region
- `clientIdPrefix`: Required prefix for client IDs (`public-`)
- `topicPrefix`: Required prefix for topics (`public/`)

## Usage

### Run the Client

```bash
uv run python iot_public_broadcast_ws.py \
  --endpoint a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com \
  --region us-east-1 \
  --identity-pool-id us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931 \
  --topic public/broadcast \
  --message "Hello, World!"
```

### Environment Variables

You can also set environment variables:

```bash
export IOT_ENDPOINT_HOST=a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com
export AWS_REGION=us-east-1
export IDENTITY_POOL_ID=us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931
export PUBLIC_TOPIC=public/broadcast

uv run python iot_public_broadcast_ws.py --message "Hello, World!"
```

### Listener Mode

To run a client that only listens (doesn't publish on connect):

```bash
uv run python iot_public_broadcast_ws.py \
  --endpoint <endpoint> \
  --region <region> \
  --identity-pool-id <pool-id> \
  --topic public/broadcast \
  --message ""
```

## How It Works

1. **Authentication**:
   - Client requests temporary credentials from Cognito Identity Pool (no login required)
   - Cognito returns AWS access keys that assume the `public-iot-unauth-role`

2. **Connection**:
   - Client uses AWS IoT SDK to create WebSocket connection
   - SDK handles SigV4 signing of the WebSocket upgrade request
   - IoT Core validates the signature and IAM permissions

3. **Pub/Sub**:
   - Client subscribes to `public/broadcast` topic
   - Any message published to this topic is broadcast to all subscribers
   - Client can publish messages that all other subscribers receive

4. **Broadcasting**:
   - AWS IoT Core acts as the message broker
   - All clients subscribed to the same topic receive published messages
   - Messages are delivered in real-time over WebSocket connections

## Security

- Client IDs **must** start with `public-` (enforced by IAM policy)
- Topics **must** be under `public/*` (enforced by IAM policy)
- No authentication required - suitable only for public, non-sensitive use cases
- Credentials are temporary (1 hour expiry)

## Infrastructure Updates

To modify the infrastructure:

1. Edit `stlv_app.py`
2. Deploy changes:
   ```bash
   cd /workspaces/stelvio/_tunnel/stlvapp-tunnel && uv run stlv deploy
   ```

## Dependencies

The client requires:
- `boto3` - AWS SDK for Python (Cognito authentication)
- `awsiotsdk` - AWS IoT Device SDK v2 (WebSocket connection)
- `awscrt` - AWS Common Runtime (underlying transport)

Install with:
```bash
uv pip install boto3 awsiotsdk
```

## Testing

We've successfully tested:
- ✅ Single client connection
- ✅ Message publishing and receiving
- ✅ Multiple clients broadcasting to each other
- ✅ Client receives its own published messages (loopback)
- ✅ Real-time message delivery

### Run the Broadcast Demo

To see multiple clients in action:

```bash
cd /workspaces/stelvio/_tunnel/stlvapp-tunnel
uv run python demo_broadcast.py
```

This will:
1. Start 3 listener clients
2. Wait for them to connect and subscribe
3. Send a broadcast message
4. Show all 3 clients receiving the message ✅

Expected output:
```
[Client 1] ✅ Received: 🎉 Hello from the broadcaster! This is a test message.
[Client 2] ✅ Received: 🎉 Hello from the broadcaster! This is a test message.
[Client 3] ✅ Received: 🎉 Hello from the broadcaster! This is a test message.
```

## Example Session

```
[setup] endpoint=a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com region=us-east-1 client_id=public-c4acf148
[debug] caller_identity: arn:aws:sts::535368238919:assumed-role/public-iot-unauth-role-2d9b4e4/CognitoIdentityCredentials
[debug] access_key=ASIAX...
[connecting]
[connected]
[subscribing] public/broadcast
[subscribed] public/broadcast
[publishing] public/broadcast: Hello, World!
[published]
[listening] Press Ctrl+C to exit...
[message] topic=public/broadcast payload=Hello, World!
```

## Troubleshooting

### 403 Forbidden Error
- Verify IAM policy allows `iot:Connect`, `iot:Publish`, `iot:Subscribe`, `iot:Receive`
- Ensure client ID starts with `public-`
- Check that topic is under `public/*`
- Verify Cognito Identity Pool allows unauthenticated identities

### Connection Timeout
- Check IoT endpoint is correct (use outputs from `stlv outputs`)
- Verify network connectivity to AWS IoT Core
- Ensure system clock is accurate (SigV4 signing is time-sensitive)

### No Messages Received
- Verify client is subscribed before messages are published
- Check that both publisher and subscriber use the same topic
- Ensure subscriber remains connected (doesn't exit immediately)

## Files

- `stlv_app.py` - Pulumi infrastructure definition
- `iot_public_broadcast_ws.py` - WebSocket client implementation (main client)
- `demo_broadcast.py` - **Multi-client broadcast demo** (shows broadcasting in action)
- `test_iot_sdk.py` - Alternative test client (similar functionality)
- `demo_broadcast.sh` - Shell-based demo (may have output buffering issues)
- `functions/incoming.py` - REST API Lambda function (separate feature)

## Future Enhancements

Possible improvements:
- Add authenticated access using Cognito User Pools
- Implement topic-based access control per user
- Add message persistence/replay capabilities
- Create web frontend for browser-based clients
- Add rate limiting and quota management
