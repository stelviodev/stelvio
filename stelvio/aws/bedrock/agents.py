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
            f"{self.name}-bedrock-agent-role",
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

        # Attach a policy to that role so that the agent (and Bedrock) can invoke Lambda and use foundation models
        agent_role_policy = pulumi_aws.iam.RolePolicy(
            f"{self.name}-bedrock-agent-policy",
            role=agent_role.id,
            policy=pulumi_aws.iam.get_policy_document(
                statements=[
                    # Allow Bedrock to invoke Lambda action group functions
                    {
                        "effect": "Allow",
                        "actions": [
                            "lambda:InvokeFunction"
                        ],
                        "resources": "*"
                    },
                    # Allow Bedrock to list/get its own resources
                    {
                        "effect": "Allow",
                        "actions": [
                            "bedrock:List*",
                            "bedrock:Get*",
                            "bedrock:InvokeAgent"
                        ],
                        "resources": "*"
                    },
                    # Allow Bedrock to invoke foundation models
                    {
                        "sid": "AmazonBedrockAgentBedrockFoundationModelPolicy",
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
        # 2. Define the Bedrock Agent
        # -------------------------------
        agent = pulumi_aws.bedrock.AgentAgent(
            f"{self.name}-bedrock-agent",
            agent_name=f"{self.name}-agent",
            foundation_model="amazon.nova-pro-v1:0",
            agent_resource_role_arn=agent_role.arn,
            instruction="You are a helpful AI agent. Use get_current_time to fetch the current time.",
            opts=pulumi.ResourceOptions(
                depends_on=[self.function.resources.function]
            ),
        )

        # -------------------------------
        # 3. Grant Bedrock permission to invoke the Lambda (resource-based policy)
        # -------------------------------
        lambda_permission = pulumi_aws.lambda_.Permission(
            f"{self.name}-bedrock-lambda-permission",
            action="lambda:InvokeFunction",
            function=self.function.resources.function.name,
            principal="bedrock.amazonaws.com",
            source_arn=agent.agent_arn,
        )

        # -------------------------------
        # 4. Define AgentAgentActionGroup linking to the Lambda function
        # -------------------------------
        action_group = pulumi_aws.bedrock.AgentAgentActionGroup(
            f"{self.name}-bedrock-agent-action-group",
            action_group_name=f"{self.name}-action-group",
            agent_id=agent.id,
            agent_version="DRAFT",
            skip_resource_in_use_check=True,
            action_group_executor={
                "lambda_": self.function.resources.function.arn
            },
            opts=pulumi.ResourceOptions(
                depends_on=[
                    self.function.resources.function,
                    lambda_permission
                ]
            ),
            # Define function schema for the action group
            function_schema={
                "member_functions": {
                    "functions": [
                        {
                            "name": "get_current_time",
                            "description": "Get the current time in UTC ISO format",
                            "parameters": []
                        }
                    ]
                }
            }
        )

        # Export useful outputs
        pulumi.export(f"{self.name}_agent_id", agent.id)
        pulumi.export(f"{self.name}_agent_arn", agent.agent_arn)
        pulumi.export(f"{self.name}_action_group_id", action_group.id)


        return AgentResources(
            agent=agent,
            action_group=action_group,
        )
