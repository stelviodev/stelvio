#!/bin/bash
# filepath: /workspaces/stelvio/watch_stream.sh

STREAM_ARN="arn:aws:dynamodb:us-east-1:535368238919:table/stlvapp-vscode-messages-125cad6/stream/2025-10-25T10:38:46.769"

# Get the first shard ID
SHARD_ID=$(aws dynamodbstreams describe-stream --stream-arn "$STREAM_ARN" --query 'StreamDescription.Shards[0].ShardId' --output text)

# Get initial shard iterator
SHARD_ITERATOR=$(aws dynamodbstreams get-shard-iterator --stream-arn "$STREAM_ARN" --shard-id "$SHARD_ID" --shard-iterator-type LATEST --query 'ShardIterator' --output text)

echo "Watching DynamoDB Stream... Press Ctrl+C to stop"
echo "Stream ARN: $STREAM_ARN"
echo "Shard ID: $SHARD_ID"
echo ""

while true; do
    # Get records using current iterator
    RESULT=$(aws dynamodbstreams get-records --shard-iterator "$SHARD_ITERATOR" 2>/dev/null)
    
    if [ $? -eq 0 ]; then
        # Extract records and next iterator
        RECORDS=$(echo "$RESULT" | jq -r '.Records[]? | "\(.eventName): \(.dynamodb.Keys.id.S // "unknown") - \(.dynamodb.NewImage.message.S // "no message")"' 2>/dev/null)
        NEXT_ITERATOR=$(echo "$RESULT" | jq -r '.NextShardIterator // empty' 2>/dev/null)
        
        # Print any records found
        if [ ! -z "$RECORDS" ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') - New records:"
            echo "$RECORDS" | sed 's/^/  /'
            echo ""
        fi
        
        # Update iterator for next poll
        if [ ! -z "$NEXT_ITERATOR" ] && [ "$NEXT_ITERATOR" != "null" ]; then
            SHARD_ITERATOR="$NEXT_ITERATOR"
        else
            echo "No more records available in this shard"
            break
        fi
    else
        echo "Error reading from stream, retrying in 5 seconds..."
        sleep 5
    fi
    
    sleep 2
done