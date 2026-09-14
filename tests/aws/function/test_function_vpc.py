import re

import pulumi
from pytest import mark, param, raises

from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc
from tests.aws.pulumi_mocks import TP, R, tid, tn

VPC_NAME = "main_vpc"
FN_NAME = "client"

PRIVATE_SUBNET_IDS = [
    tid(TP + f"{VPC_NAME}-private-subnet-a"),
    tid(TP + f"{VPC_NAME}-private-subnet-b"),
]

VPC_AZ2_COUNTS = {
    R.VPC: 1,
    R.INTERNET_GATEWAY: 1,
    R.SUBNET: 6,
    R.ROUTE_TABLE: 6,
    R.ROUTE_TABLE_ASSOCIATION: 6,
}

FUNCTION_VPC_COUNTS = {
    R.FUNCTION: 1,
    R.ROLE: 1,
    R.ROLE_POLICY_ATTACHMENT: 2,
    R.SECURITY_GROUP: 1,
}

VPC_EXECUTION_ROLE_ARN = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"


@mark.parametrize(
    ("vpc", "got"),
    [param("nope", "str", id="str"), param(42, "int", id="int")],
)
def test_function_raises_when_vpc_wrong_type(vpc, got):
    error = f"`vpc` must be a Vpc instance, got {got}"
    with raises(TypeError, match=re.escape(error)):
        Function(FN_NAME, handler="functions/simple.handler", vpc=vpc)


def test_function_in_vpc(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return Function(FN_NAME, handler="functions/simple.handler", vpc=vpc).resources

    deploy()

    pulumi_mocks.assert_res(
        FN_NAME,
        R.FUNCTION,
        {
            "vpcConfig": {
                "subnetIds": PRIVATE_SUBNET_IDS,
                "securityGroupIds": [tid(TP + f"{FN_NAME}-sg")],
            }
        },
        partial=True,
    )
    pulumi_mocks.assert_res(
        f"{FN_NAME}-sg",
        R.SECURITY_GROUP,
        {
            "description": "Managed by Pulumi",
            "vpcId": tid(TP + VPC_NAME),
            "egress": [
                {
                    "cidrBlocks": ["0.0.0.0/0"],
                    "fromPort": 0,
                    "protocol": "-1",
                    "toPort": 0,
                }
            ],
            "tags": {"Name": TP + f"{FN_NAME}-sg"},
        },
    )
    pulumi_mocks.assert_res(
        f"{FN_NAME}-vpc-execution-r-p-attachment",
        R.ROLE_POLICY_ATTACHMENT,
        {
            "role": tn(TP + f"{FN_NAME}-r"),
            "policyArn": VPC_EXECUTION_ROLE_ARN,
        },
    )
    pulumi_mocks.assert_res_counts(VPC_AZ2_COUNTS | FUNCTION_VPC_COUNTS)


def test_function_dev_mode_skips_vpc(pulumi_mocks, project_cwd, dev_mode_context):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        fn = Function(FN_NAME, handler="functions/simple.handler", vpc=vpc)
        return vpc.resources, fn.resources

    deploy()

    fn = pulumi_mocks.assert_res(FN_NAME, R.FUNCTION)
    assert fn.inputs.get("vpcConfig") is None
    attachments = pulumi_mocks.created(R.ROLE_POLICY_ATTACHMENT)
    assert all(a.inputs.get("policyArn") != VPC_EXECUTION_ROLE_ARN for a in attachments)
    pulumi_mocks.assert_res_counts(
        VPC_AZ2_COUNTS
        | {
            R.FUNCTION: 1,
            R.ROLE: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
        }
    )


def test_function_without_vpc_has_no_vpc_config(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        return Function(FN_NAME, handler="functions/simple.handler").resources

    deploy()

    fn = pulumi_mocks.assert_res(FN_NAME, R.FUNCTION)
    assert fn.inputs.get("vpcConfig") is None
    pulumi_mocks.assert_res_counts(
        {
            R.FUNCTION: 1,
            R.ROLE: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
        }
    )


def test_http_api_route_passes_vpc(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        api = HttpApi("api")
        api.route("GET", "/todos", "functions/simple.handler", vpc=vpc)
        return api.resources

    deploy()

    route_fn = "api-functions-simple_handler"
    pulumi_mocks.assert_res(
        route_fn,
        R.FUNCTION,
        {
            "vpcConfig": {
                "subnetIds": PRIVATE_SUBNET_IDS,
                "securityGroupIds": [tid(TP + f"{route_fn}-sg")],
            }
        },
        partial=True,
    )
    pulumi_mocks.assert_res_counts(
        VPC_AZ2_COUNTS
        | {
            R.SECURITY_GROUP: 1,
            R.FUNCTION: 1,
            R.ROLE: 2,
            R.ROLE_POLICY_ATTACHMENT: 2,
            R.HTTP_API: 1,
            R.API_ACCOUNT: 2,
            R.LOG_GROUP: 1,
            R.HTTP_API_STAGE: 1,
            R.HTTP_API_INTEGRATION: 1,
            R.LAMBDA_PERMISSION: 1,
            R.HTTP_API_ROUTE: 1,
        }
    )


def test_function_customize_security_group(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC_NAME)
        return Function(
            FN_NAME,
            handler="functions/simple.handler",
            vpc=vpc,
            customize={"security_group": {"tags": {"customized": "security_group"}}},
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        f"{FN_NAME}-sg",
        R.SECURITY_GROUP,
        {"tags": {"customized": "security_group"}},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(VPC_AZ2_COUNTS | FUNCTION_VPC_COUNTS)


@pulumi.runtime.test
def test_function_resources_security_group_is_the_lambda_security_group(pulumi_mocks, project_cwd):
    vpc = Vpc(VPC_NAME)
    fn = Function(FN_NAME, handler="functions/simple.handler", vpc=vpc)

    def check(sg_id):
        assert sg_id == tid(TP + f"{FN_NAME}-sg")

    return fn.resources.security_group.id.apply(check)


@pulumi.runtime.test
def test_function_resources_security_group_is_none_without_vpc(pulumi_mocks, project_cwd):
    fn = Function(FN_NAME, handler="functions/simple.handler")
    r = fn.resources

    def check(_):
        assert r.security_group is None

    return r.function.id.apply(check)


@pulumi.runtime.test
def test_function_security_group_parented_to_function(pulumi_mocks, project_cwd):
    vpc = Vpc(VPC_NAME)
    fn = Function(FN_NAME, handler="functions/simple.handler", vpc=vpc)
    security_group = fn.resources.security_group

    def check(urn):
        assert "::stelvio:aws:Function$" in urn

    return security_group.urn.apply(check)
