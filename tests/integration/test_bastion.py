"""Live checks for deployment-owned bastions and their recorded AWS identities."""

import pytest

from stelvio import export_output
from stelvio.aws.document_db import DocumentDb
from stelvio.aws.vpc import Vpc
from stelvio.stack_outputs import read_network_manifest

from .assert_bastion import (
    assert_bastion_bootstrap,
    assert_bastion_online,
    assert_document_db_ingress_from_bastion,
)
from .assert_helpers import _boto3_session
from .assert_vpc import get_security_group
from .export_helpers import export_document_db, export_vpc

pytestmark = pytest.mark.integration_vpc


def test_deployed_bastion_document_db_and_network_metadata(stelvio_env):
    def infra():
        vpc = Vpc("net", az=2, bastion=True)
        database = DocumentDb("todos", vpc=vpc)
        export_vpc(vpc)
        export_document_db(database)
        export_output("bastion_instance_id", vpc.resources.bastion.id)
        export_output("bastion_security_group_id", vpc.resources.bastion_security_group.id)
        export_output("app_security_group_id", vpc._app_security_group.id)

    outputs = stelvio_env.deploy(infra)
    state = {"checkpoint": {"latest": {"resources": stelvio_env.export_resources()}}}
    # Normal consumption of this deployment record starts with the future stlv tunnel start.
    manifest = read_network_manifest(state)
    assert len(manifest.vpcs) == 1
    vpc = manifest.vpcs[0]
    assert vpc.vpc_id == outputs["vpc_net_id"]
    assert vpc.bastion_instance_id == outputs["bastion_instance_id"]
    assert manifest.enabled_vpcs == (vpc,)
    assert set(vpc.route_subnet_ids) == set(
        outputs["vpc_net_private_subnet_ids"] + outputs["vpc_net_isolated_subnet_ids"]
    )

    session = _boto3_session()
    assert session.client("sts").get_caller_identity()["Account"] == vpc.account
    ec2 = session.client("ec2")
    aws_vpc = ec2.describe_vpcs(VpcIds=[vpc.vpc_id])["Vpcs"][0]
    assert aws_vpc["VpcId"] == vpc.vpc_id
    subnets = ec2.describe_subnets(SubnetIds=list(vpc.route_subnet_ids))["Subnets"]
    assert {subnet["SubnetId"] for subnet in subnets} == set(vpc.route_subnet_ids)
    assert {subnet["VpcId"] for subnet in subnets} == {vpc.vpc_id}
    instance = ec2.describe_instances(InstanceIds=[vpc.bastion_instance_id])["Reservations"][0][
        "Instances"
    ][0]
    assert instance["InstanceId"] == vpc.bastion_instance_id
    assert instance["VpcId"] == vpc.vpc_id
    assert {group["GroupId"] for group in instance["SecurityGroups"]} == {
        outputs["bastion_security_group_id"]
    }
    assert_bastion_online(vpc.bastion_instance_id)
    assert_bastion_bootstrap(vpc.bastion_instance_id)

    assert len(manifest.resources) == 1
    database = manifest.resources[0]
    assert database.vpc == vpc.identity
    assert database.cluster_id == outputs["document_db_todos_cluster_id"]
    cluster = session.client("docdb").describe_db_clusters(
        DBClusterIdentifier=database.cluster_id
    )["DBClusters"][0]
    assert cluster["DBClusterIdentifier"] == database.cluster_id
    assert {group["VpcSecurityGroupId"] for group in cluster["VpcSecurityGroups"]} == {
        outputs["document_db_todos_security_group_id"]
    }
    db_group = get_security_group(outputs["document_db_todos_security_group_id"])
    assert db_group["VpcId"] == vpc.vpc_id
    assert_document_db_ingress_from_bastion(
        outputs["document_db_todos_security_group_id"],
        bastion_security_group_id=outputs["bastion_security_group_id"],
        app_security_group_id=outputs["app_security_group_id"],
        port=cluster["Port"],
    )
