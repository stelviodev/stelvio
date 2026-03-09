# AppSync authentication type strings (maps to Pulumi's authentication_type)
AUTH_TYPE_API_KEY = "API_KEY"
AUTH_TYPE_IAM = "AWS_IAM"
AUTH_TYPE_COGNITO = "AMAZON_COGNITO_USER_POOLS"
AUTH_TYPE_OIDC = "OPENID_CONNECT"
AUTH_TYPE_LAMBDA = "AWS_LAMBDA"

# Data source type strings
DS_TYPE_LAMBDA = "AWS_LAMBDA"
DS_TYPE_DYNAMO = "AMAZON_DYNAMODB"
DS_TYPE_HTTP = "HTTP"
DS_TYPE_RDS = "RELATIONAL_DATABASE"
DS_TYPE_OPENSEARCH = "AMAZON_OPENSEARCH_SERVICE"
DS_TYPE_NONE = "NONE"

# APPSYNC_JS runtime config
APPSYNC_JS_RUNTIME = "APPSYNC_JS"
APPSYNC_JS_RUNTIME_VERSION = "1.0.0"

# Default passthrough JS for NONE data source (when no code= is provided)
NONE_PASSTHROUGH_CODE = """\
export function request(ctx) {
    return { payload: ctx.args };
}

export function response(ctx) {
    return ctx.result;
}
"""
