import json
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from typing import final

import pulumi
import pulumi_aws

from stelvio import context
from stelvio.aws.function.function import Function
from stelvio.aws.s3.s3 import Bucket
from stelvio.component import Component, safe_name


@dataclass(frozen=True)
class AgentResources:
    agent: pulumi_aws.bedrock.AgentAgent
    action_group: pulumi_aws.bedrock.AgentAgentActionGroup
    # lambda_invoke_role: pulumi_aws.iam.Role
    # lambda_permission: pulumi_aws.lambda_.Permission


@final
class Agent(Component[AgentResources]):
    """
    AWS Bedrock Agent component with Lambda function integration.
    
    IMPORTANT: Due to AWS Bedrock limitations, action groups must be disabled before 
    they can be deleted. If you encounter deletion errors, you can:
    
    1. Use the provided helper script: ./disable_bedrock_actions.sh <agent-id>
    2. Manually disable action groups via AWS CLI before running `stlv destroy`
    
    Args:
        name: Agent name
        function: Lambda function to be invoked by the agent
    """
    
    def __init__(
        self,
        name: str,
        function: Function,
    ):
        super().__init__(name)
        self.function = function

    def _create_resources(self) -> AgentResources:
        # -------------------------------
        # 1. IAM role for the agent itself
        # -------------------------------
        agent_role = pulumi_aws.iam.Role(
            "cccArrAgentRole",
            assume_role_policy=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "Service": "bedrock.amazonaws.com"
                        },
                        "Action": "sts:AssumeRole"
                    }
                ]
            })
        )

        # Attach a policy to that role so that the agent (and Bedrock) can e.g. invoke action groups, etc.
        agent_role_policy = pulumi_aws.iam.RolePolicy(
            "cccArrAgentRolePolicy",
            role=agent_role.id,
            policy=pulumi_aws.iam.get_policy_document(
                statements=[
                    # allow Bedrock to invoke Lambda action group functions
                    {
                        "effect": "Allow",
                        "actions": [
                            "lambda:InvokeFunction"
                        ],
                        "resources": "*"  # you can tighten this to the specific Lambda ARN(s)
                    },
                    # allow Bedrock to list/get its own resources, e.g. agents, action groups
                    {
                        "effect": "Allow",
                        "actions": [
                            "bedrock:List*",
                            "bedrock:Get*",
                            "bedrock:InvokeAgent"
                        ],
                        "resources": "*"
                    },
                    {
                        "sid": "AmazonBedrockAgentBedrockFoundationModelPolicyProd",
                        "effect": "Allow",
                        "actions": [
                            "bedrock:InvokeModel",
                            "bedrock:InvokeModelWithResponseStream"
                        ],
                        "resources": [
                            "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0",
                            "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"
                        ]
                    }
                ]
            ).json
        )

        # -------------------------------
        # 2. Define the Bedrock AgentAgent
        # -------------------------------
        agent = pulumi_aws.bedrock.AgentAgent(
            "ccc-arr",
            agent_name="ccc-arr",
            foundation_model="amazon.nova-pro-v1:0",
            agent_resource_role_arn=agent_role.arn,
            # optionally you could set a prompt / instruction, etc.
            instruction="You are agent ccc-arr. Use get_current_time to fetch the current time.",
            # (other optional settings as needed)
        )

        # -------------------------------
        # 3. Lambda function “get_current_time”
        # -------------------------------
        lambda_role = pulumi_aws.iam.Role(
            "getCurrentTimeLambdaRole",
            assume_role_policy=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {
                            "Service": "lambda.amazonaws.com"
                        },
                        "Action": "sts:AssumeRole"
                    }
                ]
            })
        )

        # Attach basic execution policy (e.g. logs) plus any needed permissions
        lambda_role_policy = pulumi_aws.iam.RolePolicy(
            "getCurrentTimeLambdaRolePolicy",
            role=lambda_role.id,
            policy=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "logs:CreateLogGroup",
                            "logs:CreateLogStream",
                            "logs:PutLogEvents"
                        ],
                        "Resource": "*"
                    }
                ]
            })
        )

        # The lambda function code: simple Python returning current time
        # You might package this differently (zip, s3, etc.). Here is an inline example using asset archive.
        lambda_fn = pulumi_aws.lambda_.Function(
            "get_current_time",
            runtime="python3.11",
            handler="index.lambda_handler",
            role=lambda_role.arn,
            code=pulumi.AssetArchive({
                # The local "index.py" file
                "index.py": pulumi.StringAsset(
                    """
from typing import Dict, Any
from http import HTTPStatus


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    try:
        action_group = event['actionGroup']
        function = event['function']
        message_version = event.get('messageVersion', '1.0')
        parameters = event.get('parameters', [])

        # Execute your business logic here. For more information, 
        # refer to: https://docs.aws.amazon.com/bedrock/latest/userguide/agents-lambda.html

        import datetime
        current_time = datetime.datetime.utcnow().isoformat() + "Z"

        response_body = {
            'TEXT': {
                'body': f'The current time is {current_time}'
            }
        }
        action_response = {
            'actionGroup': action_group,
            'function': function,
            'functionResponse': {
                'responseBody': response_body
            }
        }
        response = {
            'response': action_response,
            'messageVersion': message_version
        }

        return response

    except KeyError as e:
        return {
            'statusCode': HTTPStatus.BAD_REQUEST,
            'body': f'Error: {str(e)}'
        }
    except Exception as e:
        return {
            'statusCode': HTTPStatus.INTERNAL_SERVER_ERROR,
            'body': 'Internal server error'
        }

        """
                )
            })
        )

        # -------------------------------
        # 4. Grant Bedrock permission to invoke the Lambda (resource-based policy)
        # -------------------------------
        # As per AWS docs, your Lambda needs a resource-based policy allowing the Bedrock service (via the agent role) to call it. :contentReference[oaicite:0]{index=0}
        lambda_invocation_permission = pulumi_aws.lambda_.Permission(
            "allow_bedrock_invoke_get_current_time",
            action="lambda:InvokeFunction",
            function=lambda_fn.name,
            principal="bedrock.amazonaws.com",
            # optionally restrict to the agent role ARN:
            # source_arn = agent_role.arn
        )

        # -------------------------------
        # 5. Define AgentAgentActionGroup linking to that Lambda
        # -------------------------------
        action_group = pulumi_aws.bedrock.AgentAgentActionGroup(
            "cccArrActionGroup",
            action_group_name="get_current_time_group",
            agent_id=agent.id,
            agent_version="DRAFT",
            skip_resource_in_use_check=True,
            action_group_executor={
                "lambda_": lambda_fn.arn
            },
            # Define a minimal function schema so Bedrock knows about “get_current_time”
            function_schema={
                "member_functions": {
                    "functions": [
                        {
                            "name": "get_current_time",
                            "description": "Return the current time (UTC, ISO)",
                            "parameters": []
                        }
                    ]
                }
            }
        )

        # (Optionally, output ARNs or IDs)
        pulumi.export("agent_id", agent.id)
        pulumi.export("lambda_arn", lambda_fn.arn)
        pulumi.export("action_group_arn", action_group.id)

        pulumi.export("agent_role_policy", agent_role_policy.id)
        pulumi.export("lambda_role_policy", lambda_role_policy.id)
        pulumi.export("lambda_invocation_permission", lambda_invocation_permission.id)


        return AgentResources(
            agent=agent,
            action_group=action_group,
            # lambda_invoke_role=lambda_invoke_role,
            # lambda_permission=lambda_permission,
        )
