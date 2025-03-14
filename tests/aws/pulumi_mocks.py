import json
from typing import Any

from pulumi.runtime import MockCallArgs, MockResourceArgs, Mocks

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
        # Add debug output
        print("\nInitializing new PulumiTestMocks instance\n")

    def new_resource(self, args: MockResourceArgs) -> tuple[str, dict[str, Any]]:
        print(f"NEW RESOURCE: {args.name} -- {args.typ} --  {args.inputs}\n")

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
            output_props["id"] = f"resource-{args.name}"
        elif args.typ == "aws:apigateway/account:Account":
            ...
        elif args.typ == "aws:dynamodb/table:Table":
            output_props["arn"] = f"arn:aws:dynamodb:{region}:{account_id}:table/{name}"

        return resource_id, output_props

    def call(self, args: MockCallArgs) -> tuple[dict, list[tuple[str, str]] | None]:
        print(f"CALL:  {args.token} {args.args}\n")
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

    def created_integrations(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/integration:Integration", name)

    def created_deployments(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/deployment:Deployment", name)

    def created_stages(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/stage:Stage", name)

    def created_permissions(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:lambda/permission:Permission", name)

    def created_api_accounts(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:apigateway/account:Account", name)

    def created_dynamo_tables(self, name: str | None = None) -> list[MockResourceArgs]:
        return self._filter_created("aws:dynamodb/table:Table", name)
