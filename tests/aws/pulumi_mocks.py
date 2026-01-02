import json
from typing import Any

from pulumi.runtime import MockCallArgs, MockResourceArgs, Mocks

from stelvio.cloudflare.dns import CloudflarePulumiResourceAdapter
from stelvio.dns import Dns, Record

ROOT_RESOURCE_ID = "root-resource-id"
DEFAULT_REGION = "us-east-1"
ACCOUNT_ID = "123456789012"
TEST_USER = "test-user"
SAMPLE_API_ID = "12345abcde"


# test id
def tid(name: str) -> str:
    return name + "-test-id"


# test name
def tn(name: str) -> str:
    return name + "-test-name"


class PulumiTestMocks(Mocks):
    """Base Pulumi test mocks for all AWS resource testing."""

    def __init__(self):
        super().__init__()
        self.created_resources: list[MockResourceArgs] = []

    def new_resource(self, args: MockResourceArgs) -> tuple[str, dict[str, Any]]:  # noqa: PLR0912 C901 PLR0915
        self.created_resources.append(args)
        resource_id = tid(args.name)
        name = tn(args.name)
        output_props = args.inputs | {"name": name}

        region = DEFAULT_REGION
        account_id = ACCOUNT_ID

        # Lambda resources
        if args.typ == "aws:lambda/function:Function":
            arn = f"arn:aws:lambda:{region}:{account_id}:function:{name}"
            output_props["arn"] = arn
            output_props["invoke_arn"] = (
                f"arn:aws:apigateway:{region}:lambda:path/2015-03-31/functions/{arn}/invocations"
            )
        elif args.typ == "aws:lambda/functionUrl:FunctionUrl":
            output_props["function_url"] = f"https://{resource_id}.lambda-url.{region}.on.aws/"
        # IAM resources
        elif args.typ == "aws:iam/role:Role":
            output_props["arn"] = f"arn:aws:iam::{account_id}:role/{name}"
        elif args.typ == "aws:iam/policy:Policy":
            output_props["arn"] = f"arn:aws:iam::{account_id}:policy/{name}"
        # API Gateway resources
        elif args.typ == "aws:apigateway/restApi:RestApi":
            output_props["arn"] = f"arn:aws:apigateway:us-east-1::/restapis/{SAMPLE_API_ID}"
            output_props["execution_arn"] = (
                f"arn:aws:execute-api:{region}:{account_id}:{SAMPLE_API_ID}"
            )
            output_props["root_resource_id"] = ROOT_RESOURCE_ID
        elif args.typ == "aws:apigateway/stage:Stage":
            output_props["invokeUrl"] = (
                f"https://{args.inputs['restApi']}.execute-api.{region}.amazonaws.com/{args.inputs['stageName']}"
            )
        elif args.typ == "aws:apigateway/resource:Resource":
            output_props["arn"] = (
                f"arn:aws:apigateway:{region}::/restapis/{SAMPLE_API_ID}/resources/{resource_id}"
            )
        elif args.typ == "aws:apigateway/account:Account":
            ...
        elif args.typ == "aws:dynamodb/table:Table":
            output_props["arn"] = f"arn:aws:dynamodb:{region}:{account_id}:table/{name}"
            # Add stream ARN if stream is enabled
            if args.inputs.get("streamEnabled"):
                output_props["stream_arn"] = (
                    f"arn:aws:dynamodb:{region}:{account_id}:table/{name}/stream/2025-01-01T00:00:00.000"
                )
        elif args.typ == "aws:lambda/eventSourceMapping:EventSourceMapping":
            output_props["arn"] = (
                f"arn:aws:lambda:{region}:{account_id}:event-source-mapping:{resource_id}"
            )
        # S3 Bucket resource
        elif args.typ == "aws:s3/bucket:Bucket":
            output_props["arn"] = f"arn:aws:s3:::{name}"
            output_props["bucket"] = name
            output_props["bucket_regional_domain_name"] = f"{name}.s3.{region}.amazonaws.com"
        # S3 Bucket Object resource
        elif args.typ == "aws:s3/bucketObject:BucketObject":
            output_props["arn"] = (
                f"arn:aws:s3:::{args.inputs.get('bucket', 'unknown-bucket')}"
                f"/{args.inputs.get('key', 'unknown-key')}"
            )
            output_props["etag"] = f"etag-{resource_id}"
        # S3 Bucket Public Access Block
        elif args.typ == "aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock":
            output_props["bucket"] = args.inputs.get("bucket", name)
        # LayerVersion resource
        elif args.typ == "aws:lambda/layerVersion:LayerVersion":
            # LayerVersion ARN includes the name and version number (mocked as 1)
            output_props["arn"] = f"arn:aws:lambda:{region}:{account_id}:layer:{name}:1"
            output_props["layer_arn"] = f"arn:aws:lambda:{region}:{account_id}:layer:{name}"
            output_props["version"] = "1"
        # ACM Certificate resource
        elif args.typ == "aws:acm/certificate:Certificate":
            output_props["arn"] = f"arn:aws:acm:{region}:{account_id}:certificate/{resource_id}"
            output_props["domain_validation_options"] = [
                {
                    "resource_record_name": f"_test."
                    f"{args.inputs.get('domain_name', 'example.com')}",
                    "resource_record_type": "CNAME",
                    "resource_record_value": f"test-validation."
                    f"{args.inputs.get('domain_name', 'example.com')}",
                }
            ]
        # ACM Certificate Validation resource
        elif args.typ == "aws:acm/certificateValidation:CertificateValidation":
            output_props["certificate_arn"] = args.inputs.get("certificate_arn")
        # API Gateway Domain Name resource
        elif args.typ == "aws:apigateway/domainName:DomainName":
            output_props["cloudfront_domain_name"] = "d123456789.cloudfront.net"
            output_props["domain_name"] = args.inputs.get("domain_name")
        # API Gateway Base Path Mapping resource
        elif args.typ == "aws:apigateway/basePathMapping:BasePathMapping":
            output_props["base_path"] = args.inputs.get("base_path", "")
        # Route53 Record resource
        elif args.typ == "aws:route53/record:Record":
            output_props["fqdn"] = args.inputs.get("name", "example.com")
        # CloudFront resources
        elif args.typ == "aws:cloudfront/distribution:Distribution":
            output_props["arn"] = f"arn:aws:cloudfront::{account_id}:distribution/{resource_id}"
            output_props["domain_name"] = f"{resource_id}.cloudfront.net"
            output_props["hosted_zone_id"] = "Z2FDTNDATAQYW2"  # CloudFront's hosted zone ID
        elif args.typ == "aws:cloudfront/originAccessControl:OriginAccessControl":
            output_props["etag"] = f"ETAG{resource_id}"
        elif args.typ == "aws:cloudfront/function:Function":
            output_props["arn"] = f"arn:aws:cloudfront::{account_id}:function/{name}"
            output_props["etag"] = f"ETAG{resource_id}"
        elif args.typ == "aws:s3/bucketPolicy:BucketPolicy":
            output_props["policy"] = args.inputs.get("policy", "{}")
        # EventBridge resources
        elif args.typ == "aws:cloudwatch/eventRule:EventRule":
            output_props["arn"] = f"arn:aws:events:{region}:{account_id}:rule/{name}"
        elif args.typ == "aws:cloudwatch/eventTarget:EventTarget":
            output_props["arn"] = (
                f"arn:aws:events:{region}:{account_id}:rule/"
                f"{args.inputs.get('rule', 'unknown')}/targets/{resource_id}"
            )
        # CloudFlare Record resource (for DNS mocking)
        elif args.typ == "cloudflare:index/record:Record":
            output_props["hostname"] = args.inputs.get("name", "example.com")
            output_props["content"] = args.inputs.get("content", "127.0.0.1")

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
            return {"name": "us-east-1", "description": "US East (N. Virginia)"}, []

        return {}, []

    def _filter_created(self, typ: str, name: str | None = None) -> list[MockResourceArgs]:
        return [r for r in self.created_resources if r.typ == typ and (not name or r.name == name)]

    # Lambda resource helpers
    def created_functions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:lambda/function:Function", name)

    def created_function_urls(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:lambda/functionUrl:FunctionUrl", name)

    def created_role_policy_attachments(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:iam/rolePolicyAttachment:RolePolicyAttachment", name)

    def created_roles(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:iam/role:Role", name)

    def created_policies(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:iam/policy:Policy", name)

    # API Gateway resource helpers
    def created_rest_apis(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/restApi:RestApi", name)

    def created_api_resources(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/resource:Resource", name)

    def created_methods(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/method:Method", name)

    def created_method_responses(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/methodResponse:MethodResponse", name)

    def created_integrations(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/integration:Integration", name)

    def created_integration_responses(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/integrationResponse:IntegrationResponse", name)

    def created_gateway_responses(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/response:Response", name)

    def created_deployments(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/deployment:Deployment", name)

    def created_stages(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/stage:Stage", name)

    def created_permissions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:lambda/permission:Permission", name)

    def created_api_accounts(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/account:Account", name)

    def created_authorizers(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/authorizer:Authorizer", name)

    def created_dynamo_tables(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:dynamodb/table:Table", name)

    # S3 resource helpers
    def created_s3_buckets(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:s3/bucket:Bucket", name)

    def created_s3_bucket_objects(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:s3/bucketObject:BucketObject", name)

    def created_s3_public_access_blocks(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:s3/bucketPublicAccessBlock:BucketPublicAccessBlock", name)

    # Layer resource helper
    def created_layer_versions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:lambda/layerVersion:LayerVersion", name)

    # Custom domain resource helpers
    def created_certificates(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:acm/certificate:Certificate", name)

    def created_certificate_validations(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:acm/certificateValidation:CertificateValidation", name)

    def created_domain_names(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/domainName:DomainName", name)

    def created_base_path_mappings(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/basePathMapping:BasePathMapping", name)

    def created_dns_records(self, name: str | None = None) -> list[MockResourceArgs]:
        # This covers both Route53 and Cloudflare records
        route53_records = self._filter_created("aws:route53/record:Record", name)
        cloudflare_records = self._filter_created("cloudflare:index/record:Record", name)
        return route53_records + cloudflare_records

    # CloudFront resource helpers
    def created_cloudfront_distributions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:cloudfront/distribution:Distribution", name)

    def created_origin_access_controls(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:cloudfront/originAccessControl:OriginAccessControl", name)

    def created_cloudfront_functions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:cloudfront/function:Function", name)

    def created_bucket_policies(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:s3/bucketPolicy:BucketPolicy", name)

    # EventBridge resource helpers
    def created_event_rules(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:cloudwatch/eventRule:EventRule", name)

    def created_event_targets(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:cloudwatch/eventTarget:EventTarget", name)


class MockDns(Dns):
    """Mock DNS provider that mimics CloudflareDns interface"""

    def __init__(self):
        self.zone_id = "test-zone-id"
        self.created_records = []

    def create_record(
        self, resource_name: str, name: str, record_type: str, value: str, ttl: int = 1
    ) -> Record:
        """Create a mock DNS record following CloudflareDns pattern"""
        import pulumi_cloudflare

        record = pulumi_cloudflare.Record(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            content=value,
            ttl=ttl,
        )
        self.created_records.append((resource_name, name, record_type, value, ttl))
        return CloudflarePulumiResourceAdapter(record)

    def create_caa_record(
        self, resource_name: str, name: str, record_type: str, content: str, ttl: int = 1
    ) -> Record:
        """Create a mock CAA DNS record following CloudflareDns pattern"""
        import pulumi_cloudflare

        validation_record = pulumi_cloudflare.Record(
            resource_name,
            zone_id=self.zone_id,
            name=name,
            type=record_type,
            content=content,
            ttl=ttl,
        )
        self.created_records.append((resource_name, name, record_type, content, ttl))
        return CloudflarePulumiResourceAdapter(validation_record)
