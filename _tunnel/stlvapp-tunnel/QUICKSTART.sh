#!/bin/bash
# Quick reference for using the IoT WebSocket broadcast system

# Deployment outputs
echo "=== Deployment Info ==="
cd /workspaces/stelvio/_tunnel/stlvapp-tunnel && uv run stlv outputs | grep -E "iotWssUrl|identityPoolId|region|clientIdPrefix|topicPrefix"

echo ""
echo "=== Quick Start ==="
echo ""
echo "Run a listener (Terminal 1):"
echo "cd /workspaces/stelvio/_tunnel/stlvapp-tunnel"
echo "uv run python iot_public_broadcast_ws.py \\"
echo "  --endpoint a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com \\"
echo "  --region us-east-1 \\"
echo "  --identity-pool-id us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931 \\"
echo "  --topic public/broadcast \\"
echo "  --message \"\""
echo ""
echo "Send a message (Terminal 2):"
echo "cd /workspaces/stelvio/_tunnel/stlvapp-tunnel"
echo "uv run python iot_public_broadcast_ws.py \\"
echo "  --endpoint a1omtjrlih4wxu-ats.iot.us-east-1.amazonaws.com \\"
echo "  --region us-east-1 \\"
echo "  --identity-pool-id us-east-1:fb817d42-1d97-4fd6-a332-c4699f28f931 \\"
echo "  --topic public/broadcast \\"
echo "  --message \"Your message here\""
