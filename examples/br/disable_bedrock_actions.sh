#!/bin/bash

# Bedrock Agent Destruction Helper Script
# This script handles the proper sequence for destroying Bedrock agents
# by disabling action groups before attempting deletion.

set -e

AGENT_ID="$1"
REGION="${2:-us-east-1}"

if [ -z "$AGENT_ID" ]; then
    echo "Usage: $0 <agent-id> [region]"
    echo "Example: $0 H10JOOUAID us-east-1"
    exit 1
fi

echo "Preparing to destroy Bedrock agent: $AGENT_ID in region: $REGION"

# Get all action groups for the agent
echo "Fetching action groups for agent..."
ACTION_GROUPS=$(aws bedrock-agent list-agent-action-groups \
    --agent-id "$AGENT_ID" \
    --agent-version "DRAFT" \
    --region "$REGION" \
    --query 'actionGroupSummaries[].actionGroupId' \
    --output text)

if [ -n "$ACTION_GROUPS" ]; then
    echo "Found action groups: $ACTION_GROUPS"
    
    for ACTION_GROUP_ID in $ACTION_GROUPS; do
        echo "Disabling action group: $ACTION_GROUP_ID"
        
        # Get current action group details
        DETAILS=$(aws bedrock-agent get-agent-action-group \
            --agent-id "$AGENT_ID" \
            --agent-version "DRAFT" \
            --action-group-id "$ACTION_GROUP_ID" \
            --region "$REGION")
        
        ACTION_GROUP_NAME=$(echo "$DETAILS" | jq -r '.agentActionGroup.actionGroupName')
        LAMBDA_ARN=$(echo "$DETAILS" | jq -r '.agentActionGroup.actionGroupExecutor.lambda')
        
        echo "Action group name: $ACTION_GROUP_NAME"
        echo "Lambda ARN: $LAMBDA_ARN"
        
        # Create temporary function schema file
        TEMP_SCHEMA=$(mktemp)
        echo "$DETAILS" | jq '.agentActionGroup.functionSchema.functions' > "$TEMP_SCHEMA"
        
        # Disable the action group
        aws bedrock-agent update-agent-action-group \
            --agent-id "$AGENT_ID" \
            --agent-version "DRAFT" \
            --action-group-id "$ACTION_GROUP_ID" \
            --action-group-name "$ACTION_GROUP_NAME" \
            --action-group-state "DISABLED" \
            --function-schema "file://$TEMP_SCHEMA" \
            --action-group-executor "lambda=$LAMBDA_ARN" \
            --region "$REGION"
        
        # Clean up temp file
        rm "$TEMP_SCHEMA"
        
        echo "Action group $ACTION_GROUP_ID disabled successfully"
    done
    
    echo "Waiting 5 seconds for changes to propagate..."
    sleep 5
    
else
    echo "No action groups found for agent $AGENT_ID"
fi

echo "All action groups have been disabled. You can now run 'uv run stlv destroy' safely."