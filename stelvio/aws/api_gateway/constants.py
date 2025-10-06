from enum import Enum
from typing import Literal

ROUTE_MAX_PARAMS = 10
ROUTE_MAX_LENGTH = 8192
HTTP_METHODS = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
API_GATEWAY_LOGS_POLICY = (
    "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
)


# These are methods supported by api gateway
class HTTPMethod(Enum):
    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"
    ANY = "ANY"


HTTPMethodLiteral = Literal["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "ANY", "*"]

type HTTPMethodInput = (
    str | HTTPMethodLiteral | HTTPMethod | list[str | HTTPMethodLiteral | HTTPMethod]
)

ApiEndpointType = Literal["regional", "edge"]
DEFAULT_STAGE_NAME = "v1"
DEFAULT_ENDPOINT_TYPE: ApiEndpointType = "regional"
API_GATEWAY_ROLE_NAME = "StelvioAPIGatewayPushToCloudWatchLogsRole"
