#!/usr/bin/env python3
"""
Test script to verify Bedrock agent functionality
"""

import boto3
import json
import time

def test_bedrock_agent():
    # Initialize Bedrock client
    bedrock_agent_runtime = boto3.client('bedrock-agent-runtime', region_name='us-east-1')
    
    # Agent details (replace with your actual values)
    agent_id = "H10JOOUAID"  # From the deployment output
    agent_alias_id = "TSTALIASID"  # Default test alias
    
    try:
        # Test the agent with a simple query
        response = bedrock_agent_runtime.invoke_agent(
            agentId=agent_id,
            agentAliasId=agent_alias_id,
            sessionId=f"test-session-{int(time.time())}",
            inputText="What is the current time?"
        )
        
        # Process the response
        completion = ""
        for event in response.get('completion', []):
            chunk = event.get('chunk', {})
            if 'bytes' in chunk:
                completion += chunk['bytes'].decode('utf-8')
        
        print("Agent Response:")
        print(completion)
        return True
        
    except Exception as e:
        print(f"Error testing agent: {e}")
        return False

if __name__ == "__main__":
    test_bedrock_agent()