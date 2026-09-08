import json
from collections import Counter
from enum import StrEnum
from functools import cache
from importlib import import_module
from typing import Any

import pulumi_cloudflare
from pulumi import ResourceOptions
from pulumi.runtime import MockCallArgs, MockResourceArgs, Mocks

from stelvio.cloudflare.dns import CloudflarePulumiResourceAdapter
from stelvio.dns import Dns, Record

ROOT_RESOURCE_ID = "root-resource-id"
DEFAULT_REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TEST_USER = "test-user"
SAMPLE_API_ID = "12345abcde"

# Test prefix: "{app}-{env}-" for the AppContext(name="test", env="test") set in conftest
TP = "test-test-"


class R(StrEnum):
    """Pulumi type tokens of resources used in tests."""

    # EC2 / VPC
    VPC = "aws:ec2/vpc:Vpc"
    SUBNET = "aws:ec2/subnet:Subnet"
    INTERNET_GATEWAY = "aws:ec2/internetGateway:InternetGateway"
    ROUTE_TABLE = "aws:ec2/routeTable:RouteTable"
    ROUTE_TABLE_ASSOCIATION = "aws:ec2/routeTableAssociation:RouteTableAssociation"
    ROUTE = "aws:ec2/route:Route"
    NAT_GATEWAY = "aws:ec2/natGateway:NatGateway"
    EIP = "aws:ec2/eip:Eip"
    # Lambda
    FUNCTION = "aws:lambda/function:Function"
    FUNCTION_URL = "aws:lambda/functionUrl:FunctionUrl"
    EVENT_SOURCE_MAPPING = "aws:lambda/eventSourceMapping:EventSourceMapping"
    LAYER_VERSION = "aws:lambda/layerVersion:LayerVersion"
    LAMBDA_PERMISSION = "aws:lambda/permission:Permission"
    # IAM
    ROLE = "aws:iam/role:Role"
    POLICY = "aws:iam/policy:Policy"
    ROLE_POLICY = "aws:iam/rolePolicy:RolePolicy"
    ROLE_POLICY_ATTACHMENT = "aws:iam/rolePolicyAttachment:RolePolicyAttachment"
    # API Gateway
    REST_API = "aws:apigateway/restApi:RestApi"
    API_STAGE = "aws:apigateway/stage:Stage"
    API_RESOURCE = "aws:apigateway/resource:Resource"
    API_ACCOUNT = "aws:apigateway/account:Account"
    API_METHOD = "aws:apigateway/method:Method"
    API_METHOD_RESPONSE = "aws:apigateway/methodResponse:MethodResponse"
    API_INTEGRATION = "aws:apigateway/integration:Integration"
    API_INTEGRATION_RESPONSE = "aws:apigateway/integrationResponse:IntegrationResponse"
    API_GATEWAY_RESPONSE = "aws:apigateway/response:Response"
    API_DEPLOYMENT = "aws:apigateway/deployment:Deployment"
    API_AUTHORIZER = "aws:apigateway/authorizer:Authorizer"
    API_DOMAIN_NAME = "aws:apigateway/domainName:DomainName"
    API_BASE_PATH_MAPPING = "aws:apigateway/basePathMapping:BasePathMapping"
    # API Gateway v2
    HTTP_API = "aws:apigatewayv2/api:Api"
    HTTP_API_STAGE = "aws:apigatewayv2/stage:Stage"
    HTTP_API_INTEGRATION = "aws:apigatewayv2/integration:Integration"
    HTTP_API_ROUTE = "aws:apigatewayv2/route:Route"
    HTTP_API_AUTHORIZER = "aws:apigatewayv2/authorizer:Authorizer"
    HTTP_API_DOMAIN_NAME = "aws:apigatewayv2/domainName:DomainName"
    HTTP_API_MAPPING = "aws:apigatewayv2/apiMapping:ApiMapping"
    LOG_GROUP = "aws:cloudwatch/logGroup:LogGroup"
    # DynamoDB
    DYNAMO_TABLE = "aws:dynamodb/table:Table"
    # S3
    BUCKET = "aws:s3/bucket:Bucket"
    BUCKET_OBJECT = "aws:s3/bucketObject:BucketObject"
    BUCKET_PUBLIC_ACCESS_BLOCK = "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock"
    BUCKET_POLICY = "aws:s3/bucketPolicy:BucketPolicy"
    BUCKET_NOTIFICATION = "aws:s3/bucketNotification:BucketNotification"
    # ACM
    CERTIFICATE = "aws:acm/certificate:Certificate"
    CERTIFICATE_VALIDATION = "aws:acm/certificateValidation:CertificateValidation"
    # DNS
    ROUTE53_RECORD = "aws:route53/record:Record"
    CLOUDFLARE_RECORD = "cloudflare:index/record:Record"
    # CloudFront
    DISTRIBUTION = "aws:cloudfront/distribution:Distribution"
    ORIGIN_ACCESS_CONTROL = "aws:cloudfront/originAccessControl:OriginAccessControl"
    CLOUDFRONT_FUNCTION = "aws:cloudfront/function:Function"
    # SQS / SNS
    QUEUE = "aws:sqs/queue:Queue"
    QUEUE_POLICY = "aws:sqs/queuePolicy:QueuePolicy"
    TOPIC = "aws:sns/topic:Topic"
    TOPIC_SUBSCRIPTION = "aws:sns/topicSubscription:TopicSubscription"
    TOPIC_POLICY = "aws:sns/topicPolicy:TopicPolicy"
    # EventBridge
    EVENT_RULE = "aws:cloudwatch/eventRule:EventRule"
    EVENT_TARGET = "aws:cloudwatch/eventTarget:EventTarget"
    # AppSync
    GRAPHQL_API = "aws:appsync/graphQLApi:GraphQLApi"
    APPSYNC_DATA_SOURCE = "aws:appsync/dataSource:DataSource"
    APPSYNC_RESOLVER = "aws:appsync/resolver:Resolver"
    APPSYNC_FUNCTION = "aws:appsync/function:Function"
    APPSYNC_API_KEY = "aws:appsync/apiKey:ApiKey"
    APPSYNC_DOMAIN_NAME = "aws:appsync/domainName:DomainName"
    APPSYNC_DOMAIN_ASSOCIATION = "aws:appsync/domainNameApiAssociation:DomainNameApiAssociation"
    # SES
    EMAIL_IDENTITY = "aws:sesv2/emailIdentity:EmailIdentity"
    CONFIGURATION_SET = "aws:sesv2/configurationSet:ConfigurationSet"
    # Cognito
    USER_POOL = "aws:cognito/userPool:UserPool"
    USER_POOL_CLIENT = "aws:cognito/userPoolClient:UserPoolClient"
    IDENTITY_PROVIDER = "aws:cognito/identityProvider:IdentityProvider"
    IDENTITY_POOL = "aws:cognito/identityPool:IdentityPool"
    IDENTITY_POOL_ROLE_ATTACHMENT = (
        "aws:cognito/identityPoolRoleAttachment:IdentityPoolRoleAttachment"
    )
    USER_POOL_DOMAIN = "aws:cognito/userPoolDomain:UserPoolDomain"
    # Providers
    AWS_PROVIDER = "pulumi:providers:aws"


# test id
def tid(name: str) -> str:
    return name + "-test-id"


# test name
def tn(name: str) -> str:
    return name + "-test-name"


@cache
def _output_props(typ: str) -> frozenset[str]:
    """Real output property names of a resource type, from the installed provider SDK.

    Outputs are properties on the resource class, so the provider package is the source of
    truth for which resources actually have e.g. `name`/`arn` outputs — no hand-kept lists.
    """
    try:
        provider, mod_path, cls_name = typ.split(":")
        module = mod_path.split("/")[0]
        module = {"lambda": "lambda_", "index": ""}.get(module, module)
        mod = import_module(f"pulumi_{provider}.{module}".rstrip("."))
        cls = getattr(mod, cls_name)
    except (ValueError, ImportError, AttributeError):
        return frozenset()
    return frozenset(n for n in dir(cls) if isinstance(getattr(cls, n, None), property))


# Fake per-type outputs. Placeholders: {region}, {account}, {id}=tid(pulumi name),
# {name}=tn(pulumi name), {in[key]}=that resource's input (camelCase key; missing
# required input raises KeyError — deliberately loud). String leaves are `.format`ted,
# nested dicts/lists recurse, other values pass through. Resources with a real `arn`
# output but no "arn" entry here get a generic `arn:aws:{service}:...:generic-arn/{id}`
# fallback. Only conditional outputs (e.g. DynamoDB's streamArn) live in `new_resource`.
# NOTE: output property names must use camelCase (Pulumi's wire format, see
# https://www.pulumi.com/docs/iac/guides/testing/unit/). The Python SDK currently also
# resolves snake_case, but that's undocumented leniency — don't rely on it.
OUTPUT_TEMPLATES: dict[str, dict[str, Any]] = {
    # EC2 / VPC
    R.EIP: {"allocationId": "eipalloc-{id}"},
    # Lambda
    R.FUNCTION: {
        "arn": "arn:aws:lambda:{region}:{account}:function:{name}",
        "invokeArn": "arn:aws:apigateway:{region}:lambda:path/2015-03-31/functions/"
        "arn:aws:lambda:{region}:{account}:function:{name}/invocations",
    },
    R.FUNCTION_URL: {"functionUrl": "https://{id}.lambda-url.{region}.on.aws/"},
    R.EVENT_SOURCE_MAPPING: {"arn": "arn:aws:lambda:{region}:{account}:event-source-mapping:{id}"},
    R.LAYER_VERSION: {
        "arn": "arn:aws:lambda:{region}:{account}:layer:{name}:1",
        "layerArn": "arn:aws:lambda:{region}:{account}:layer:{name}",
        "version": "1",
    },
    # IAM
    R.ROLE: {"arn": "arn:aws:iam::{account}:role/{name}"},
    R.POLICY: {"arn": "arn:aws:iam::{account}:policy/{name}"},
    # API Gateway
    R.REST_API: {
        "arn": f"arn:aws:apigateway:{{region}}::/restapis/{SAMPLE_API_ID}",
        "executionArn": f"arn:aws:execute-api:{{region}}:{{account}}:{SAMPLE_API_ID}",
        "rootResourceId": ROOT_RESOURCE_ID,
    },
    R.API_RESOURCE: {
        "arn": f"arn:aws:apigateway:{{region}}::/restapis/{SAMPLE_API_ID}/resources/{{id}}"
    },
    R.API_STAGE: {
        "invokeUrl": "https://{in[restApi]}.execute-api.{region}.amazonaws.com/{in[stageName]}"
    },
    R.API_DOMAIN_NAME: {
        "cloudfrontDomainName": "d123456789.cloudfront.net",
        "regionalDomainName": "d-{id}.execute-api.{region}.amazonaws.com",
    },
    # DynamoDB
    R.DYNAMO_TABLE: {"arn": "arn:aws:dynamodb:{region}:{account}:table/{name}"},
    # S3
    R.BUCKET: {
        "arn": "arn:aws:s3:::{name}",
        "bucket": "{name}",
        "bucketRegionalDomainName": "{name}.s3.{region}.amazonaws.com",
    },
    R.BUCKET_OBJECT: {"arn": "arn:aws:s3:::{in[bucket]}/{in[key]}", "etag": "etag-{id}"},
    # ACM
    R.CERTIFICATE: {
        "arn": "arn:aws:acm:{region}:{account}:certificate/{id}",
        "domainValidationOptions": [
            {
                "resourceRecordName": "_test.{in[domainName]}",
                "resourceRecordType": "CNAME",
                "resourceRecordValue": "test-validation.{in[domainName]}",
            }
        ],
    },
    # DNS
    R.ROUTE53_RECORD: {"fqdn": "{in[name]}", "name": "{in[name]}"},
    R.CLOUDFLARE_RECORD: {"hostname": "{in[name]}", "name": "{in[name]}"},
    # CloudFront
    R.DISTRIBUTION: {
        "arn": "arn:aws:cloudfront::{account}:distribution/{id}",
        "domainName": "{id}.cloudfront.net",
        "hostedZoneId": "Z2FDTNDATAQYW2",  # CloudFront's hosted zone ID
    },
    R.ORIGIN_ACCESS_CONTROL: {"etag": "ETAG{id}"},
    R.CLOUDFRONT_FUNCTION: {
        "arn": "arn:aws:cloudfront::{account}:function/{name}",
        "etag": "ETAG{id}",
    },
    # SQS / SNS
    R.QUEUE: {
        "arn": "arn:aws:sqs:{region}:{account}:{name}",
        "url": "https://sqs.{region}.amazonaws.com/{account}/{name}",
    },
    R.TOPIC: {"arn": "arn:aws:sns:{region}:{account}:{name}"},
    R.TOPIC_SUBSCRIPTION: {"arn": "arn:aws:sns:{region}:{account}:{name}"},
    # EventBridge
    R.EVENT_RULE: {"arn": "arn:aws:events:{region}:{account}:rule/{name}"},
    R.EVENT_TARGET: {"arn": "arn:aws:events:{region}:{account}:rule/{in[rule]}/targets/{id}"},
    # AppSync
    R.GRAPHQL_API: {
        "arn": "arn:aws:appsync:{region}:{account}:apis/appsync-{id}",
        "id": "appsync-{id}",
        "uris": {
            "GRAPHQL": "https://appsync-{id}.appsync-api.{region}.amazonaws.com/graphql",
            "REALTIME": "wss://appsync-{id}.appsync-realtime-api.{region}.amazonaws.com/graphql",
        },
    },
    R.APPSYNC_DATA_SOURCE: {
        "arn": "arn:aws:appsync:{region}:{account}:apis/test-api/datasources/{name}"
    },
    R.APPSYNC_FUNCTION: {
        "arn": "arn:aws:appsync:{region}:{account}:apis/test-api/functions/{id}",
        "functionId": "fn-{id}",
    },
    R.APPSYNC_RESOLVER: {
        "arn": "arn:aws:appsync:{region}:{account}:apis/test-api/types/"
        "{in[type]}/resolvers/{in[field]}"
    },
    R.APPSYNC_API_KEY: {"id": "apikey-{id}", "key": "da2-test-api-key-{id}"},
    R.APPSYNC_DOMAIN_NAME: {
        "arn": "arn:aws:appsync:{region}:{account}:domainnames/{in[domainName]}",
        "appsyncDomainName": "{id}.appsync-api.{region}.amazonaws.com",
    },
    R.APPSYNC_DOMAIN_ASSOCIATION: {"id": "{id}"},
    # Cognito
    R.USER_POOL: {
        "arn": "arn:aws:cognito-idp:{region}:{account}:userpool/{region}_{id}",
        "id": "{region}_{id}",
    },
    R.USER_POOL_CLIENT: {"id": "{id}-client-id", "clientSecret": "{id}-client-secret"},
    R.IDENTITY_POOL: {
        "arn": "arn:aws:cognito-identity:{region}:{account}:identitypool/{region}:{id}",
        "id": "{region}:{id}",
    },
    R.USER_POOL_DOMAIN: {
        "cloudfrontDistribution": "d111111abcdef8.cloudfront.net",
        "cloudfrontDistributionZoneId": "Z2FDTNDATAQYW2",
    },
    # SES
    R.EMAIL_IDENTITY: {"arn": "arn:aws:ses:{region}:{account}:identity/{in[emailIdentity]}"},
    # CloudWatch
    R.LOG_GROUP: {"arn": "arn:aws:logs:{region}:{account}:log-group:{name}:*"},
    # Providers
    R.AWS_PROVIDER: {"region": "{in[region]}"},
}


def _fill(template: Any, subs: dict[str, str]) -> Any:
    if isinstance(template, str):
        return template.format_map(subs)
    if isinstance(template, dict):
        return {k: _fill(v, subs) for k, v in template.items()}
    if isinstance(template, list):
        return [_fill(v, subs) for v in template]
    return template


def _add_http_api_outputs(
    args: MockResourceArgs,
    output_props: dict[str, Any],
    resource_id: str,
    account_context: tuple[str, str],
    api_protocols: dict[str, str],
) -> None:
    region, account_id = account_context
    if args.typ == R.HTTP_API:
        output_props["arn"] = f"arn:aws:apigateway:{region}::/apis/{resource_id}"
        output_props["executionArn"] = f"arn:aws:execute-api:{region}:{account_id}:{resource_id}"
        api_protocols[resource_id] = args.inputs["protocolType"]
    elif args.typ == R.HTTP_API_STAGE:
        stage_name = args.inputs.get("name", "$default")
        api_id = args.inputs.get("apiId", args.inputs.get("api_id", "unknown"))
        protocol = api_protocols.get(api_id)
        scheme = "wss" if protocol == "WEBSOCKET" else "https"
        invoke_url = f"{scheme}://{api_id}.execute-api.{region}.amazonaws.com"
        if protocol == "WEBSOCKET" or stage_name != "$default":
            invoke_url += f"/{stage_name}"
        output_props["invokeUrl"] = invoke_url
    elif args.typ == R.HTTP_API_DOMAIN_NAME:
        output_props["domainName"] = args.inputs.get("domainName", "api.example.com")
        output_props["domainNameConfiguration"] = {
            "targetDomainName": f"d-{resource_id}.execute-api.{region}.amazonaws.com",
            "hostedZoneId": "Z2FDTNDATAQYW2",
            "endpointType": "REGIONAL",
            "securityPolicy": "TLS_1_2",
            "certificateArn": args.inputs.get("domainNameConfiguration", {}).get(
                "certificateArn", ""
            ),
        }


class PulumiTestMocks(Mocks):
    """Base Pulumi test mocks for all AWS resource testing."""

    def __init__(self):
        super().__init__()
        self.created_resources: list[MockResourceArgs] = []
        self.api_protocols: dict[str, str] = {}

    def new_resource(self, args: MockResourceArgs) -> tuple[str, dict[str, Any]]:
        self.created_resources.append(args)
        resource_id = tid(args.name)
        name = tn(args.name)
        output_props = dict(args.inputs)

        region = DEFAULT_REGION
        account_id = ACCOUNT_ID

        # `name` output only for resources that really have one (per provider SDK)
        if "name" in _output_props(args.typ):
            output_props["name"] = name

        if templates := OUTPUT_TEMPLATES.get(args.typ):
            subs = {
                "region": region,
                "account": account_id,
                "id": resource_id,
                "name": name,
                "in": {**args.inputs, "region": args.inputs.get("region", DEFAULT_REGION)},
            }
            output_props |= _fill(templates, subs)

        # Conditional outputs can't be templated
        if args.typ == R.DYNAMO_TABLE and args.inputs.get("streamEnabled"):
            output_props["streamArn"] = (
                f"arn:aws:dynamodb:{region}:{account_id}:table/{name}/stream/2025-01-01T00:00:00.000"
            )

        # Real `arn` output (per provider SDK) but nothing above set one. Deliberately
        # marked "generic-arn" so it can't be mistaken for a real format: fine for wiring
        # assertions, but the moment a test/component cares about ARN shape, look the
        # real format up and add it to OUTPUT_TEMPLATES.
        if "arn" in _output_props(args.typ) and "arn" not in output_props:
            service = args.typ.split(":")[1].split("/")[0]
            output_props["arn"] = (
                f"arn:aws:{service}:{region}:{account_id}:generic-arn/{resource_id}"
            )

        _add_http_api_outputs(
            args, output_props, resource_id, (region, account_id), self.api_protocols
        )

        return resource_id, output_props

    def call(self, args: MockCallArgs) -> tuple[dict, list[tuple[str, str]] | None]:
        # print(f"CALL:  {args.token} {args.args}\n")
        if args.token == "aws:iam/getPolicyDocument:getPolicyDocument":  # noqa: S105
            statements_str = json.dumps(args.args["statements"])
            return {"json": statements_str}, []
        if args.token == "aws:index/getCallerIdentity:getCallerIdentity":  # noqa: S105
            return {
                "accountId": ACCOUNT_ID,
                "arn": f"arn:aws:iam::{ACCOUNT_ID}:user/{TEST_USER}",
                "userId": f"{TEST_USER}-id",
            }, []
        if args.token == "aws:index/getRegion:getRegion":  # noqa: S105
            return {
                "name": "us-east-1",
                "region": "us-east-1",
                "description": "US East (N. Virginia)",
            }, []
        if args.token == "aws:index/getAvailabilityZones:getAvailabilityZones":  # noqa: S105
            return {"names": ["us-east-1a", "us-east-1b", "us-east-1c"]}, []

        return {}, []

    def created(self, typ: R, name: str | None = None) -> list[MockResourceArgs]:
        """All created resources of type `typ`, optionally narrowed to one exact name.

        For a known name prefer `assert_res`, which also pins the inputs. Use this when
        you genuinely need every resource of a type, e.g. to compare exact name sets.
        """
        return [r for r in self.created_resources if r.typ == typ and (not name or r.name == name)]

    # Lambda resource helpers
    def created_functions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.FUNCTION, name)

    def created_function_urls(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.FUNCTION_URL, name)

    def created_role_policy_attachments(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.ROLE_POLICY_ATTACHMENT, name)

    def created_roles(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.ROLE, name)

    def created_policies(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.POLICY, name)

    # API Gateway resource helpers
    def created_rest_apis(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.REST_API, name)

    def created_api_resources(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_RESOURCE, name)

    def created_methods(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_METHOD, name)

    def created_method_responses(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_METHOD_RESPONSE, name)

    def created_integrations(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_INTEGRATION, name)

    def created_integration_responses(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_INTEGRATION_RESPONSE, name)

    def created_gateway_responses(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_GATEWAY_RESPONSE, name)

    def created_deployments(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_DEPLOYMENT, name)

    def created_stages(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_STAGE, name)

    def created_permissions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.LAMBDA_PERMISSION, name)

    def created_api_accounts(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_ACCOUNT, name)

    def created_authorizers(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_AUTHORIZER, name)

    def created_dynamo_tables(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.DYNAMO_TABLE, name)

    # S3 resource helpers
    def created_s3_buckets(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.BUCKET, name)

    def created_s3_bucket_objects(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.BUCKET_OBJECT, name)

    def created_s3_public_access_blocks(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.BUCKET_PUBLIC_ACCESS_BLOCK, name)

    # Layer resource helper
    def created_layer_versions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.LAYER_VERSION, name)

    # Custom domain resource helpers
    def created_certificates(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.CERTIFICATE, name)

    def created_certificate_validations(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.CERTIFICATE_VALIDATION, name)

    def created_domain_names(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_DOMAIN_NAME, name)

    def created_base_path_mappings(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.API_BASE_PATH_MAPPING, name)

    def created_dns_records(self, name: str | None = None) -> list[MockResourceArgs]:
        # This covers both Route53 and Cloudflare records
        route53_records = self.created(R.ROUTE53_RECORD, name)
        cloudflare_records = self.created(R.CLOUDFLARE_RECORD, name)
        return route53_records + cloudflare_records

    # CloudFront resource helpers
    def created_cloudfront_distributions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.DISTRIBUTION, name)

    def created_origin_access_controls(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.ORIGIN_ACCESS_CONTROL, name)

    def created_cloudfront_functions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.CLOUDFRONT_FUNCTION, name)

    def created_bucket_policies(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.BUCKET_POLICY, name)

    # AWS Provider helpers
    def created_providers(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.AWS_PROVIDER, name)

    # EventBridge resource helpers
    def created_event_rules(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.EVENT_RULE, name)

    def created_event_targets(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.EVENT_TARGET, name)

    # SQS resource helpers
    def created_event_source_mappings(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.EVENT_SOURCE_MAPPING, name)

    def created_queues(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.QUEUE, name)

    def created_sqs_queues(self, name: str | None = None) -> list[MockResourceArgs]:
        """Alias for created_queues for clarity."""
        return self.created_queues(name)

    def created_queue_policies(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.QUEUE_POLICY, name)

    # SNS resource helpers
    def created_topics(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.TOPIC, name)

    def created_sns_topics(self, name: str | None = None) -> list[MockResourceArgs]:
        """Alias for created_topics for clarity."""
        return self.created_topics(name)

    def created_topic_subscriptions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.TOPIC_SUBSCRIPTION, name)

    def created_topic_policies(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.TOPIC_POLICY, name)

    # DynamoDB resource helpers
    def created_dynamodb_tables(self, name: str | None = None) -> list[MockResourceArgs]:
        """Alias for created_dynamo_tables for clarity."""
        return self.created_dynamo_tables(name)

    # Cognito resource helpers
    def created_user_pools(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.USER_POOL, name)

    def created_user_pool_clients(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.USER_POOL_CLIENT, name)

    def created_identity_providers(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.IDENTITY_PROVIDER, name)

    def created_identity_pools(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.IDENTITY_POOL, name)

    def created_identity_pool_roles_attachments(
        self, name: str | None = None
    ) -> list[MockResourceArgs]:
        return self.created(R.IDENTITY_POOL_ROLE_ATTACHMENT, name)

    def created_user_pool_domains(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.USER_POOL_DOMAIN, name)

    # SES resource helpers
    def created_email_identities(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.EMAIL_IDENTITY, name)

    def created_configuration_sets(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.CONFIGURATION_SET, name)

    # S3 bucket notification resource helpers
    def created_bucket_notifications(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.BUCKET_NOTIFICATION, name)

    # AppSync resource helpers
    def created_appsync_apis(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.GRAPHQL_API, name)

    def created_appsync_data_sources(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.APPSYNC_DATA_SOURCE, name)

    def created_appsync_resolvers(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.APPSYNC_RESOLVER, name)

    def created_appsync_functions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.APPSYNC_FUNCTION, name)

    def created_appsync_api_keys(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.APPSYNC_API_KEY, name)

    def created_appsync_domain_names(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.APPSYNC_DOMAIN_NAME, name)

    def created_appsync_domain_associations(
        self, name: str | None = None
    ) -> list[MockResourceArgs]:
        return self.created(R.APPSYNC_DOMAIN_ASSOCIATION, name)

    def created_role_policies(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.ROLE_POLICY, name)

    def created_log_groups(self, name: str | None = None) -> list[MockResourceArgs]:
        return self.created(R.LOG_GROUP, name)

    # =========================================================================
    # Assertion Helpers
    # =========================================================================

    def assert_res(
        self,
        name: str,
        typ: R | None = None,
        inputs: dict[str, Any] | None = None,
        *,
        partial: bool = False,
        prefixed: bool = True,
    ) -> MockResourceArgs:
        """Assert exactly one resource named `TP + name` was created and return it.

        Optionally assert its type token and inputs (full compare, or subset with
        `partial=True`). Inputs are asserted as recorded, i.e. camelCase keys. Plain
        asserts inside rely on `pytest.register_assert_rewrite` for this module (see
        tests/conftest.py) so failures show pytest's full diff at the caller's line.
        """
        __tracebackhide__ = True
        expected_name = TP + name if prefixed else name
        found = [r for r in self.created_resources if r.name == expected_name]
        assert len(found) == 1, (
            f"'{expected_name}': created {sorted(r.name for r in self.created_resources)}"
        )
        resource = found[0]
        if typ is not None:
            assert resource.typ == typ
        if inputs is not None:
            if partial:
                assert {k: resource.inputs.get(k) for k in inputs} == inputs
            else:
                assert resource.inputs == inputs
        return resource

    def assert_no_res(self, *types: R) -> None:
        """Assert no resource of the given type(s) was created."""
        __tracebackhide__ = True
        found = [(str(r.typ), r.name) for r in self.created_resources if r.typ in types]
        assert not found

    def assert_res_counts(self, expected: dict[R, int]) -> None:
        """Assert created resource counts by type match `expected` exactly.

        Seals a test: a resource of any type not listed fails. Stelvio component
        resources and providers are ignored.
        """
        __tracebackhide__ = True
        actual = Counter(
            r.typ
            for r in self.created_resources
            if not r.typ.startswith(("stelvio:", "pulumi:providers:"))
        )
        assert actual == expected

    def assert_function_created(self, name: str) -> MockResourceArgs:
        """Assert exactly one Lambda function with the given name exists and return it."""
        functions = self.created_functions(name)
        assert len(functions) == 1, (
            f"Expected exactly 1 function named '{name}', found {len(functions)}"
        )
        return functions[0]

    def assert_user_pool_created(self, name: str) -> MockResourceArgs:
        """Assert exactly one Cognito user pool with the given name exists and return it."""
        pools = self.created_user_pools(name)
        assert len(pools) == 1, f"Expected exactly 1 user pool named '{name}', found {len(pools)}"
        return pools[0]

    def assert_user_pool_client_created(self, name: str) -> MockResourceArgs:
        """Assert exactly one Cognito user pool client with the given name exists and return it."""
        clients = self.created_user_pool_clients(name)
        assert len(clients) == 1, (
            f"Expected exactly 1 user pool client named '{name}', found {len(clients)}"
        )
        return clients[0]

    def assert_identity_provider_created(self, name: str) -> MockResourceArgs:
        """Assert exactly one Cognito identity provider with the given name exists."""
        providers = self.created_identity_providers(name)
        assert len(providers) == 1, (
            f"Expected exactly 1 identity provider named '{name}', found {len(providers)}"
        )
        return providers[0]

    def assert_identity_pool_created(self, name: str) -> MockResourceArgs:
        """Assert exactly one Cognito identity pool with the given name exists."""
        pools = self.created_identity_pools(name)
        assert len(pools) == 1, (
            f"Expected exactly 1 identity pool named '{name}', found {len(pools)}"
        )
        return pools[0]

    def assert_role_created(self, name: str) -> MockResourceArgs:
        """Assert exactly one IAM role with the given name exists and return it."""
        roles = self.created_roles(name)
        assert len(roles) == 1, f"Expected exactly 1 role named '{name}', found {len(roles)}"
        return roles[0]

    def assert_role_policy_created(self, name: str) -> MockResourceArgs:
        """Assert exactly one IAM role policy with the given name exists and return it."""
        policies = self.created_role_policies(name)
        assert len(policies) == 1, (
            f"Expected exactly 1 role policy named '{name}', found {len(policies)}"
        )
        return policies[0]

    def assert_roles_attachment_created(self, name: str) -> MockResourceArgs:
        """Assert exactly one identity pool roles attachment with the given name exists."""
        attachments = self.created_identity_pool_roles_attachments(name)
        assert len(attachments) == 1, (
            f"Expected exactly 1 roles attachment named '{name}', found {len(attachments)}"
        )
        return attachments[0]

    def assert_user_pool_domain_created(self, name: str) -> MockResourceArgs:
        """Assert exactly one Cognito user pool domain with the given name exists."""
        domains = self.created_user_pool_domains(name)
        assert len(domains) == 1, (
            f"Expected exactly 1 user pool domain named '{name}', found {len(domains)}"
        )
        return domains[0]


class MockDns(Dns):
    """Mock DNS provider that mimics CloudflareDns interface"""

    def __init__(self):
        self.zone_id = "test-zone-id"
        self.created_records = []

    def create_record(  # noqa: PLR0913
        self,
        resource_name: str,
        name: str,
        record_type: str,
        value: str,
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> Record:
        """Create a mock DNS record following CloudflareDns pattern"""
        record = pulumi_cloudflare.Record(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            content=value,
            ttl=ttl,
            opts=opts,
        )
        self.created_records.append((resource_name, name, record_type, value, ttl))
        return CloudflarePulumiResourceAdapter(record)

    def create_caa_record(  # noqa: PLR0913
        self,
        resource_name: str,
        name: str,
        record_type: str,
        content: str,
        ttl: int = 1,
        *,
        opts: ResourceOptions | None = None,
    ) -> Record:
        """Create a mock CAA DNS record following CloudflareDns pattern"""
        validation_record = pulumi_cloudflare.Record(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            content=content,
            ttl=ttl,
            opts=opts,
        )
        self.created_records.append((resource_name, name, record_type, content, ttl))
        return CloudflarePulumiResourceAdapter(validation_record)
