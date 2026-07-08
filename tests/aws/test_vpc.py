import re

import pulumi
from conftest import TP
from pytest import mark, param, raises

from stelvio.aws.vpc import NatConfig, Vpc


@mark.parametrize(
    ("nat", "error_suffix"),
    [
        param(NatConfig(ip=["1"]), "expected 2 (2 AZs)", id="two-azs-one-ip"),
        param(
            NatConfig(ip=["1", "2"], single=True),
            "expected 1 (single NAT)",
            id="single-nat-two-ips",
        ),
    ],
)
def test_vpc_raises_value_error_when_nat_ip_count_wrong(nat, error_suffix):
    error_start = "`nat.ip` must provide one Elastic IP allocation ID per NAT gateway: "
    with raises(ValueError, match=re.escape(error_start + error_suffix)):
        Vpc("main_vpc", nat=nat)


@mark.parametrize(
    ("az", "error_message"),
    [
        param(
            True,
            "`az` parameter must be `int` or `list[str]`, got bool",
            id="bool-true",
        ),
        param(
            False,
            "`az` parameter must be `int` or `list[str]`, got bool",
            id="bool-false",
        ),
        param(
            ["us-east-1a", 4],
            "When `az` is a list, each item must be a string, got 4",
            id="list-with-non-string",
        ),
        param(
            "us-east-1a",
            "`az` parameter must be `int` or `list[str]`, got",
            id="bare-string",
        ),
    ],
)
def test_vpc_raises_type_error_when_az_wrong_type(az, error_message):
    with raises(TypeError, match=re.escape(error_message)):
        Vpc("main_vpc", az=az)


@mark.parametrize(
    ("az", "error_message"),
    [
        param(0, "When `az` is a number it must be at least 1, got 0", id="zero"),
        param(-1, "When `az` is a number it must be at least 1, got -1", id="negative"),
        param(
            [],
            "When `az` is a list, you must provide at least one name.",
            id="empty-list",
        ),
        param(
            ["us-east-1a", "us-east-1a"],
            "`az` must not contain duplicate names, got",
            id="duplicates",
        ),
    ],
)
def test_vpc_raises_value_error_when_az_invalid(az, error_message):
    with raises(ValueError, match=re.escape(error_message)):
        Vpc("main_vpc", az=az)


@pulumi.runtime.test
def test_vpc_default(pulumi_mocks):
    vpc = Vpc("main_vpc")

    def check_resources(_):
        vpc_resources = pulumi_mocks.created_vpcs(TP + "main_vpc")
        assert len(vpc_resources) == 1
        vpc_resource = vpc_resources[0]
        assert vpc_resource.inputs == {
            "cidrBlock": "10.0.0.0/16",
            "enableDnsSupport": True,
            "enableDnsHostnames": True,
            "tags": {"Name": TP + "main_vpc"},
        }

    vpc.resources.vpc.arn.apply(check_resources)
