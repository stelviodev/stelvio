#! /usr/bin/env bash

curl -X POST "https://r1g9pcls4l.execute-api.us-east-1.amazonaws.com/v1/tunnel/123" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello through the curl script!"}'
