#!/bin/bash

# Activate virtual environment
if [ -f "/workspaces/demo/.venv/bin/activate" ]; then
    source /workspaces/demo/.venv/bin/activate
fi

# Export AWS credentials
if command -v aws >/dev/null 2>&1; then
    # Check if we have valid credentials or config
    # We use a subshell to avoid polluting the environment with temp vars if it fails
    _aws_creds=$(aws configure export-credentials 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$_aws_creds" ]; then
        export AWS_ACCESS_KEY_ID=$(echo "$_aws_creds" | jq -r '.AccessKeyId')
        export AWS_SECRET_ACCESS_KEY=$(echo "$_aws_creds" | jq -r '.SecretAccessKey')
        export AWS_SESSION_TOKEN=$(echo "$_aws_creds" | jq -r '.SessionToken')
    fi
    unset $_aws_creds
fi
