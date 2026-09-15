"""`Function(vpc=...)`: the Lambda joins the Vpc's subnets wearing the Vpc's shared app
security group, and its role gets the AWS-managed VPC access policy."""

import re

import pulumi
from pytest import mark, param, raises

from stelvio.aws.api_gateway import HttpApi
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc, VpcAttachment
from tests.aws.pulumi_mocks import TP, R, tid, tn

VPC = "main_vpc"
APP_SG_ID = tid(TP + f"{VPC}-app-sg")
PRIVATE_SUBNET_IDS = [tid(TP + f"{VPC}-private-subnet-{az}") for az in "ab"]
ISOLATED_SUBNET_IDS = [tid(TP + f"{VPC}-isolated-subnet-{az}") for az in "ab"]
VPC_ACCESS_POLICY_ARN = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"

# Vpc(az=2), no NAT: three tiers per AZ, one route table per subnet (IGW route inline)
VPC_COUNTS = {
    R.VPC: 1,
    R.INTERNET_GATEWAY: 1,
    R.SUBNET: 6,
    R.ROUTE_TABLE: 6,
    R.ROUTE_TABLE_ASSOCIATION: 6,
}
APP_SG_COUNTS = {R.SECURITY_GROUP: 1, R.SECURITY_GROUP_EGRESS_RULE: 1}
# basic execution + VPC access attachments; no links, so no function policy
VPC_FUNCTION_COUNTS = {R.FUNCTION: 1, R.ROLE: 1, R.ROLE_POLICY_ATTACHMENT: 2}


def assert_vpc_access_attached(pulumi_mocks, fn: str) -> None:
    pulumi_mocks.assert_res(
        f"{fn}-vpc-access-r-p-attachment",
        R.ROLE_POLICY_ATTACHMENT,
        {"role": tn(TP + f"{fn}-r"), "policyArn": VPC_ACCESS_POLICY_ARN},
    )


# each form is built from the Vpc made inside the test: a Vpc needs the app context, which
# exists only while a test runs, not at collection time
@mark.parametrize(
    ("attach", "subnet_ids"),
    [
        param(lambda vpc: vpc, PRIVATE_SUBNET_IDS, id="vpc"),
        param(lambda vpc: VpcAttachment(vpc=vpc), PRIVATE_SUBNET_IDS, id="attachment"),
        param(lambda vpc: {"vpc": vpc}, PRIVATE_SUBNET_IDS, id="dict"),
        param(
            lambda vpc: VpcAttachment(vpc=vpc, subnets="isolated"),
            ISOLATED_SUBNET_IDS,
            id="attachment-isolated",
        ),
        param(
            lambda vpc: {"vpc": vpc, "subnets": "isolated"},
            ISOLATED_SUBNET_IDS,
            id="dict-isolated",
        ),
    ],
)
def test_function_in_vpc(pulumi_mocks, project_cwd, attach, subnet_ids):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC)
        return Function("worker", handler="functions/simple.handler", vpc=attach(vpc)).resources

    deploy()

    pulumi_mocks.assert_res(
        "worker",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": subnet_ids, "securityGroupIds": [APP_SG_ID]}},
        partial=True,
    )
    assert_vpc_access_attached(pulumi_mocks, "worker")
    pulumi_mocks.assert_res_counts(VPC_COUNTS | APP_SG_COUNTS | VPC_FUNCTION_COUNTS)


def test_function_with_own_security_groups(pulumi_mocks, project_cwd):
    # bring-your-own groups replace the app SG outright: the Vpc creates nothing for it
    security_groups = [f"sg-{i}" for i in range(5)]  # the AWS limit; 6 is rejected below

    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC)
        return Function(
            "worker",
            handler="functions/simple.handler",
            vpc={"vpc": vpc, "security_groups": security_groups},
        ).resources

    deploy()

    pulumi_mocks.assert_res(
        "worker",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": PRIVATE_SUBNET_IDS, "securityGroupIds": security_groups}},
        partial=True,
    )
    pulumi_mocks.assert_res_counts(VPC_COUNTS | VPC_FUNCTION_COUNTS)


def test_functions_share_the_app_security_group(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC)
        first = Function("first", handler="functions/simple.handler", vpc=vpc)
        second = Function(
            "second",
            handler="functions/simple.handler",
            vpc={"vpc": vpc, "subnets": "isolated"},
        )
        return first.resources, second.resources

    deploy()

    for name in ("first", "second"):
        fn = pulumi_mocks.assert_res(name, R.FUNCTION)
        assert fn.inputs["vpcConfig"]["securityGroupIds"] == [APP_SG_ID]
    pulumi_mocks.assert_res_counts(
        VPC_COUNTS | APP_SG_COUNTS | {R.FUNCTION: 2, R.ROLE: 2, R.ROLE_POLICY_ATTACHMENT: 4}
    )


@mark.usefixtures("dev_mode_context")
def test_function_dev_mode_stub_stays_out_of_vpc(pulumi_mocks, project_cwd):
    # the stub must reach the websocket bridge, so it gets no vpc config; the app SG and
    # the VPC access policy still exist, so deploy <-> dev never deletes and recreates them
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC)
        return Function("worker", handler="functions/simple.handler", vpc=vpc).resources

    deploy()

    stub = pulumi_mocks.assert_res("worker", R.FUNCTION)
    assert stub.inputs["handler"] == "stlv_function_stub.handler"
    assert stub.inputs.get("vpcConfig") is None
    assert_vpc_access_attached(pulumi_mocks, "worker")
    pulumi_mocks.assert_res_counts(VPC_COUNTS | APP_SG_COUNTS | VPC_FUNCTION_COUNTS)


def test_function_without_vpc(pulumi_mocks, project_cwd):
    @pulumi.runtime.test
    def deploy():
        return Function("worker", handler="functions/simple.handler").resources

    deploy()

    fn = pulumi_mocks.assert_res("worker", R.FUNCTION)
    assert fn.inputs.get("vpcConfig") is None
    pulumi_mocks.assert_res_counts({R.FUNCTION: 1, R.ROLE: 1, R.ROLE_POLICY_ATTACHMENT: 1})


def test_http_api_route_handler_in_vpc(pulumi_mocks, project_cwd):
    # `vpc` is a FunctionConfig field, so route handlers take it like any other option
    @pulumi.runtime.test
    def deploy():
        vpc = Vpc(VPC)
        api = HttpApi("api")
        api.route("GET", "/todos", "functions/simple.handler", vpc=vpc)
        return api.resources

    deploy()

    pulumi_mocks.assert_res(
        "api-functions-simple_handler",
        R.FUNCTION,
        {"vpcConfig": {"subnetIds": PRIVATE_SUBNET_IDS, "securityGroupIds": [APP_SG_ID]}},
        partial=True,
    )
    assert_vpc_access_attached(pulumi_mocks, "api-functions-simple_handler")
    pulumi_mocks.assert_res_counts(
        VPC_COUNTS
        | APP_SG_COUNTS
        | {
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


# each case builds its input from the Vpc made inside the test, as test_function_in_vpc does;
# dict errors come back wrapped as the field's own error, like `url`
@mark.parametrize(
    ("attach", "error", "message"),
    [
        param(
            lambda _: 123,
            TypeError,
            "'vpc' must be a Vpc, a VpcAttachment, a dict, or None. Got int",
            id="not-a-vpc",
        ),
        param(
            lambda _: {"subnets": "isolated"},
            ValueError,
            "Invalid vpc configuration: VpcAttachment.__init__() missing 1 required "
            "keyword-only argument: 'vpc'",
            id="dict-without-vpc",
        ),
        param(
            lambda _: {"vpc": "main_vpc"},
            ValueError,
            "Invalid vpc configuration: 'vpc' must be a Vpc, got str",
            id="dict-vpc-not-a-vpc",
        ),
        param(
            lambda vpc: {"vpc": vpc, "subnets": "public"},
            ValueError,
            "Invalid vpc configuration: Invalid subnets 'public'. Must be 'private' or "
            "'isolated'. Lambda network interfaces never get a public IP, so a public subnet "
            "gives no internet access.",
            id="dict-public-subnets",
        ),
        param(
            lambda vpc: {"vpc": vpc, "security_groups": []},
            ValueError,
            "Invalid vpc configuration: 'security_groups' cannot be empty. Omit it to use the "
            "Vpc's app security group.",
            id="dict-empty-security-groups",
        ),
        param(
            lambda vpc: {"vpc": vpc, "security_groups": [f"sg-{i}" for i in range(6)]},
            ValueError,
            "Invalid vpc configuration: 'security_groups' can hold at most 5 IDs (AWS limit), "
            "got 6.",
            id="dict-too-many-security-groups",
        ),
        param(
            lambda vpc: {"vpc": vpc, "security_groups": "sg-1"},
            ValueError,
            "Invalid vpc configuration: 'security_groups' must be a list of security group "
            "IDs (str), got 'sg-1'",
            id="dict-security-groups-not-a-list",
        ),
        param(
            lambda vpc: {"vpc": vpc, "security_groups": ["sg-1", 2]},
            ValueError,
            "Invalid vpc configuration: 'security_groups' must be a list of security group "
            "IDs (str), got ['sg-1', 2]",
            id="dict-security-group-not-a-str",
        ),
        param(
            lambda vpc: {"vpc": vpc, "dedicated_sg": True},
            ValueError,
            "Invalid vpc configuration: VpcAttachment.__init__() got an unexpected keyword "
            "argument 'dedicated_sg'",
            id="dict-unknown-key",
        ),
    ],
)
def test_function_invalid_vpc(attach, error, message):
    vpc = Vpc(VPC)
    with raises(error, match=re.escape(message)):
        Function("worker", handler="functions/simple.handler", vpc=attach(vpc))
