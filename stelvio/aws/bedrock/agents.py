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
    lambda_invoke_role: pulumi_aws.iam.Role
    lambda_permission: pulumi_aws.lambda_.Permission


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
        # Create the assume role policy (trust policy) that allows Bedrock to assume this role
        agent_resource_role = pulumi_aws.iam.Role(
            f"{self.name}-bedrock-agent-role-2",
            assume_role_policy=pulumi.Output.all(
                pulumi_aws.get_caller_identity().account_id
            ).apply(
                lambda args: json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AmazonBedrockAgentBedrockFoundationModelPolicyProd",
                            "Effect": "Allow",
                            "Principal": {
                                "Service": "bedrock.amazonaws.com"
                            },
                            "Action": "sts:AssumeRole",
                            "Condition": {
                                "StringEquals": {
                                    "aws:SourceAccount": args[0]
                                },
                                "ArnLike": {
                                    "aws:SourceArn": f"arn:aws:bedrock:us-east-1:{args[0]}:agent/*"
                                }
                            }
                        }
                    ]
                })
            ),
        )

        # Create the IAM policy with Bedrock permissions AND Lambda invoke permissions
        bedrock_policy = pulumi_aws.iam.RolePolicy(
            f"{self.name}-bedrock-agent-policy",
            role=agent_resource_role.id,
            policy=self.function.resources.function.arn.apply(
                lambda arn: json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "AmazonBedrockAgentBedrockFoundationModelPolicyProd",
                            "Effect": "Allow",
                            "Action": [
                                "bedrock:InvokeModel",
                                "bedrock:InvokeModelWithResponseStream"
                            ],
                            "Resource": [
                                "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0",
                                "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0"
                            ]
                        },
                        {
                            "Sid": "LambdaInvokePolicy",
                            "Effect": "Allow",
                            "Action": [
                                "lambda:InvokeFunction"
                            ],
                            "Resource": arn
                        }
                    ]
                })
            ),
        )

        # Create IAM role that allows Bedrock to invoke Lambda functions (keeping for compatibility)
        lambda_invoke_role = pulumi_aws.iam.Role(
            f"{self.name}-bedrock-lambda-invoke-role",
            assume_role_policy=pulumi.Output.all().apply(
                lambda _: json.dumps({
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
            ),
        )

        # Create IAM policy for Lambda invocation (keeping for compatibility)
        lambda_invoke_policy = pulumi_aws.iam.RolePolicy(
            f"{self.name}-bedrock-lambda-invoke-policy",
            role=lambda_invoke_role.id,
            policy=self.function.resources.function.arn.apply(
                lambda arn: json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "lambda:InvokeFunction"
                            ],
                            "Resource": arn
                        }
                    ]
                })
            ),
        )

        # Create Lambda permission to allow Bedrock service to invoke the function
        lambda_permission = pulumi_aws.lambda_.Permission(
            f"{self.name}-bedrock-lambda-permission",
            statement_id="agentsInvokeFunction",
            action="lambda:invokeFunction",
            function=self.function.resources.function.name,
            principal="bedrock.amazonaws.com",
        )

        agent = pulumi_aws.bedrock.AgentAgent(
            f"{self.name}-bedrock-agent",
            agent_name=f"{self.name}-bedrock-agent-name",
            foundation_model="amazon.nova-pro-v1:0",  # Example model ID
            instruction="""You're a helpful agent. Answer all user questions according to your training. 
                            It is VERY IMPORTANT to end EVERY answer with \"Arrr\" STRICTLY! So just end every answer with \"Arr\".""",
            agent_resource_role_arn=agent_resource_role.arn,
            # Add explicit dependencies to ensure IAM policies are created first
            opts=pulumi.ResourceOptions(depends_on=[bedrock_policy, lambda_permission]),
        )

        action_group = pulumi_aws.bedrock.AgentAgentActionGroup(
            f"{self.name}-bedrock-agent-action-group",
            action_group_name=f"{self.name}-bedrock-agent-action-group-name",
            agent_id=agent.id,
            agent_version="DRAFT",
            action_group_state="ENABLED",  # Explicitly set to ENABLED for normal operation
            # ENABLED action groups must be disabled before deletion. Pulumi cannot do this automatically.
            function_schema={
                "member_functions": {
                    "functions": [{
                        "name": "get_current_time",
                        "description": "get the current time",
                        "parameters": []
                    }],
                },
            },
            action_group_executor={
                "lambda_": self.function.resources.function.arn
            },
            # Ensure Lambda permission is created before action group
            opts=pulumi.ResourceOptions(depends_on=[lambda_permission]),
        )

        pulumi.export("bedrock_agent_id", agent.id)
        pulumi.export("bedrock_agent_arn", agent.agent_arn)
        pulumi.export("bedrock_agent_action_group_id", action_group.id)
        pulumi.export("bedrock_lambda_invoke_role_arn", lambda_invoke_role.arn)
        pulumi.export("bedrock_agent_resource_role_arn", agent_resource_role.arn)


        return AgentResources(
            agent=agent,
            action_group=action_group,
            lambda_invoke_role=lambda_invoke_role,
            lambda_permission=lambda_permission,
        )
