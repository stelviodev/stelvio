# Quick Demo Guide

## See Broadcasting in Action

Run this command to see 3 clients receive a broadcast message:

```bash
cd /workspaces/stelvio/_tunnel/stlvapp-tunnel
uv run python demo_broadcast.py
```

## Expected Output

You should see:
```
[Client 1] ✅ Received: 🎉 Hello from the broadcaster! This is a test message.
[Client 2] ✅ Received: 🎉 Hello from the broadcaster! This is a test message.
[Client 3] ✅ Received: 🎉 Hello from the broadcaster! This is a test message.
```

✅ **All 3 listener clients receive the same message = Broadcasting works!**

## Manual Testing (2 Terminals)

### Terminal 1 - Listener
```bash
cd /workspaces/stelvio/_tunnel/stlvapp-tunnel
uv run python iot_public_broadcast_ws.py \
  --endpoint a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com \
  --region us-east-1 \
  --identity-pool-id us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931 \
  --topic public/broadcast \
  --message ""
```

### Terminal 2 - Broadcaster
```bash
cd /workspaces/stelvio/_tunnel/stlvapp-tunnel
uv run python iot_public_broadcast_ws.py \
  --endpoint a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com \
  --region us-east-1 \
  --identity-pool-id us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931 \
  --topic public/broadcast \
  --message "Hello from Terminal 2!"
```

You should see Terminal 1 receive: `[message] topic=public/broadcast payload=Hello from Terminal 2!`

## Custom Message

```bash
uv run python demo_broadcast.py --message "Your custom message here"
```

## Different Number of Listeners

```bash
uv run python demo_broadcast.py --num-listeners 5
```
