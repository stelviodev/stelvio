
import boto3
import time

bedrock_agent_runtime = boto3.client('bedrock-agent-runtime', region_name='us-east-1')

try:
    response = bedrock_agent_runtime.invoke_agent(
        agentId="X2OKVEICEG",
        agentAliasId="TSTALIASID",
        sessionId=f"test-{int(time.time())}",
        inputText="What is the current time?"
    )
    
    print("Success! Agent response:")
    for event in response.get('completion', []):
        chunk = event.get('chunk', {})
        if 'bytes' in chunk:
            print(chunk['bytes'].decode('utf-8'), end='')
    print()
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
