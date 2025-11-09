from stelvio.app import StelvioApp
from stelvio.aws.api_gateway import Api
from stelvio.aws.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.config import StelvioAppConfig, AwsConfig

import json
import pulumi
from pulumi import Output, ResourceOptions, FileAsset, AssetArchive
from pulumi_aws import (
    apigatewayv2,
    dynamodb,
    iam,
    lambda_ as awslambda,
    cloudwatch,
    apigateway,  # For CloudWatch logging role (needed for access logs reliability)
    iot,         # IoT Core data endpoint lookup
    cognito,     # Cognito Identity Pool for unauthenticated public access
    get_region,
    get_caller_identity,
)

app = StelvioApp("stlvapp-tunnel")

@app.config
def configuration(env: str) -> StelvioAppConfig:
    return StelvioAppConfig(
        aws=AwsConfig(
            # region="us-east-1",        # Uncomment to override AWS CLI/env var region
            # profile="your-profile",    # Uncomment to use specific AWS profile
        ),
    )

@app.run
def run() -> None:
    create()

    

def create():
    # Look up current region/account for ARNs
    region = get_region()
    caller = get_caller_identity()
    
    # Discover the IoT Core ATS data endpoint for this account/region
    iot_endpoint = iot.get_endpoint(endpoint_type="iot:Data-ATS")
    
    # Create IAM permission for Lambda to publish to IoT topics
    iot_publish_permission = AwsPermission(
        actions=["iot:Publish"],
        resources=Output.all(region.name, caller.account_id).apply(
            lambda args: [f"arn:aws:iot:{args[0]}:{args[1]}:topic/public/*"]
        ),
    )
    
    # Create the Lambda function with IoT permissions and environment variable
    incoming_function = Function(
        "incoming-handler",
        handler="functions/incoming.handler",
        environment={"IOT_ENDPOINT": iot_endpoint.endpoint_address},
    )
    
    # Manually add the IoT publish policy to the Lambda role
    iot_publish_policy = iam.RolePolicy(
        "lambda-iot-publish-policy",
        role=incoming_function.resources.role.name,
        policy=Output.all(region.name, caller.account_id).apply(
            lambda args: json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["iot:Publish", "iot:Connect", "iot:Subscribe", "iot:Receive"],
                        "Resource": [
                            f"arn:aws:iot:{args[0]}:{args[1]}:topic/public/*"
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["iot:GetThingShadow", "iot:UpdateThingShadow", "iot:DeleteThingShadow"],
                        "Resource": [
                            f"arn:aws:iot:{args[0]}:{args[1]}:thing/*"
                        ],
                    },
                ],
            })
        ),
    )
    
    # Create API Gateway and route it to the function
    incomingRequestApi = Api("incoming-request-api")
    incomingRequestApi.route("POST", "/tunnel/{channel_id}", incoming_function)

    # Provision a "public" MQTT over WebSocket endpoint via AWS IoT Core using
    # a Cognito Identity Pool that allows UNAUTHENTICATED identities.
    # Clients will:
    #   - Use the Identity Pool Id to obtain temporary AWS creds WITHOUT login
    #   - Connect to IoT Core WSS endpoint (wss://<endpoint>/mqtt) using SigV4
    #   - Use clientId starting with "public-"
    #   - Publish and Subscribe to topics under "public/*" (e.g. "public/broadcast")
    # By subscribing and publishing to the same topic, IoT Core broker will 
    # broadcast messages to all connected subscribers (simple echo/broadcast).

    # Identity Pool that permits unauthenticated identities
    identity_pool = cognito.IdentityPool(
        "public-iot-identity-pool",
        identity_pool_name="public-iot-identity-pool",
        allow_unauthenticated_identities=True,
    )

    # IAM role assumed by unauthenticated identities from this Identity Pool
    unauth_assume_role_policy = identity_pool.id.apply(
        lambda pool_id: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Federated": "cognito-identity.amazonaws.com"},
                        "Action": "sts:AssumeRoleWithWebIdentity",
                        "Condition": {
                            "StringEquals": {"cognito-identity.amazonaws.com:aud": pool_id},
                            "ForAnyValue:StringLike": {
                                "cognito-identity.amazonaws.com:amr": "unauthenticated"
                            },
                        },
                    }
                ],
            }
        )
    )

    unauth_role = iam.Role(
        "public-iot-unauth-role",
        assume_role_policy=unauth_assume_role_policy,
        description="Role for unauthenticated Cognito identities to access IoT public/* topics",
    )

    # Allow unauth identities to connect with clientId "public-*" and
    # Pub/Sub/Receive on topics under "public/*".
    # For SigV4-authenticated WebSocket connections, AWS recommends using Resource="*"
    # with IoT condition keys to scope permissions.
    # See: https://docs.aws.amazon.com/iot/latest/developerguide/authorizing-direct-aws.html
    # NOTE: Start permissive for initial connectivity validation, then tighten with conditions below
    permissive_unauth_policy_doc = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["iot:Connect", "iot:Publish", "iot:Subscribe", "iot:Receive"],
                    "Resource": "*",
                }
            ],
        }
    )

    # Intended least-privilege policy (kept for reference; flip to this after validation)
    unauth_policy_doc = Output.all(region.name, caller.account_id).apply(
        lambda args: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["iot:Connect"],
                        "Resource": [
                            f"arn:aws:iot:{args[0]}:{args[1]}:client/public-*"
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["iot:Publish", "iot:Receive"],
                        "Resource": [
                            f"arn:aws:iot:{args[0]}:{args[1]}:topic/public/*"
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["iot:Subscribe"],
                        "Resource": [
                            f"arn:aws:iot:{args[0]}:{args[1]}:topicfilter/public/*"
                        ],
                    },
                ],
            }
        )
    )

    # Apply the permissive policy initially to rule out signature vs IAM issues
    unauth_policy = iam.RolePolicy(
        "public-iot-unauth-policy",
        role=unauth_role.id,
        policy=permissive_unauth_policy_doc,
    )

    # Attach the unauth role to the identity pool
    cognito.IdentityPoolRoleAttachment(
        "public-iot-identity-pool-role-attachment",
        identity_pool_id=identity_pool.id,
        roles={"unauthenticated": unauth_role.arn},
    )

    # Use the IoT endpoint that was already discovered at the top
    wss_url = f"wss://{iot_endpoint.endpoint_address}/mqtt"

    # Export connection details for clients
    pulumi.export("iotWssUrl", wss_url)
    pulumi.export("identityPoolId", identity_pool.id)
    pulumi.export("region", region.name)
    pulumi.export("clientIdPrefix", "public-")
    pulumi.export("topicPrefix", "public/")