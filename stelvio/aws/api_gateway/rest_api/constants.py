from typing import Literal

from stelvio.aws.api_gateway.methods import HTTPMethod, HTTPMethodInput, HTTPMethodLiteral

__all__ = [
    "API_GATEWAY_LOGS_POLICY",
    "API_GATEWAY_ROLE_NAME",
    "DEFAULT_ENDPOINT_TYPE",
    "DEFAULT_STAGE_NAME",
    "HTTP_METHODS",
    "ROUTE_MAX_LENGTH",
    "ROUTE_MAX_PARAMS",
    "HTTPMethod",
    "HTTPMethodInput",
    "HTTPMethodLiteral",
]

ROUTE_MAX_PARAMS = 10
ROUTE_MAX_LENGTH = 8192
HTTP_METHODS = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
API_GATEWAY_LOGS_POLICY = (
    "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
)

ApiEndpointType = Literal["regional", "edge"]
DEFAULT_STAGE_NAME = "v1"
DEFAULT_ENDPOINT_TYPE: ApiEndpointType = "regional"
API_GATEWAY_ROLE_NAME = "StelvioAPIGatewayPushToCloudWatchLogsRole"
