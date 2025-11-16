#!/bin/bash
# Demo script to show AWS IoT broadcast functionality
# This script starts multiple clients and demonstrates message broadcasting

ENDPOINT="a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com"
REGION="us-east-1"
IDENTITY_POOL_ID="us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931"
TOPIC="public/broadcast"

echo "=== AWS IoT Core Broadcast Demo ==="
echo ""
echo "Starting 3 listener clients..."
echo ""

# Start 3 listener clients in background
for i in 1 2 3; do
  echo "Starting Client $i (listener only)..."
  timeout 30 uv run python -u iot_public_broadcast_ws.py \
    --endpoint "$ENDPOINT" \
    --region "$REGION" \
    --identity-pool-id "$IDENTITY_POOL_ID" \
    --topic "$TOPIC" \
    --message "" > "/tmp/client_$i.log" 2>&1 &
  PIDS[$i]=$!
done

echo ""
echo "Waiting for clients to connect (10 seconds)..."
sleep 10

echo ""
echo "Sending broadcast message from a 4th client..."
echo ""

# Send a broadcast message
timeout 5 uv run python -u iot_public_broadcast_ws.py \
  --endpoint "$ENDPOINT" \
  --region "$REGION" \
  --identity-pool-id "$IDENTITY_POOL_ID" \
  --topic "$TOPIC" \
  --message "🎉 Hello from the broadcaster! This message should reach all 3 listeners."

# Wait a bit for the message to be delivered
sleep 2

echo ""
echo "=== Results ==="
echo ""

# Show what each client received
for i in 1 2 3; do
  echo "Client $i output:"
  if [ -s "/tmp/client_$i.log" ]; then
    grep -E "\[message\]|\[subscribed\]|\[connected\]" "/tmp/client_$i.log" 2>/dev/null || echo "  Connected but no messages yet"
  else
    echo "  (log file empty - client may still be starting)"
  fi
  echo ""
done

echo "Full log files:"
for i in 1 2 3; do
  if [ -s "/tmp/client_$i.log" ]; then
    echo "Client $i (last 10 lines):"
    tail -10 "/tmp/client_$i.log" | sed 's/^/  /'
    echo ""
  fi
done

# Cleanup
echo "Cleaning up processes..."
for pid in "${PIDS[@]}"; do
  kill $pid 2>/dev/null
done
wait 2>/dev/null

echo ""
echo "Logs are in /tmp/client_*.log for inspection"
echo "Demo complete!"
