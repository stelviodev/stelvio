"""Live deployment checks for VPC-owned bastions and private network metadata."""

import pytest

from stelvio.aws.document_db import DocumentDb
from stelvio.aws.function import Function
from stelvio.aws.vpc import Vpc
from stelvio.stack_outputs import get_deployed_components, read_network_manifest
from stelvio.tunnel.policy import BastionPolicy

from .assert_bastion import assert_bastion_resources, assert_document_db_ingress_from_bastion
from .assert_helpers import _boto3_session, get_lambda_vpc_config
from .assert_vpc import get_security_group
from .export_helpers import export_document_db, export_function, export_vpc

pytestmark = pytest.mark.integration_vpc


def test_deployed_bastion_document_db_and_network_metadata(stelvio_env, project_dir):
    def infra():
        vpc = Vpc("net", az=2, bastion=True)
        database = DocumentDb("todos", vpc=vpc)
        function = Function("probe", handler="handlers/net_probe.main", vpc=vpc)
        export_vpc(vpc)
        export_document_db(database)
        export_function(function)

    outputs = stelvio_env.deploy(infra)
    state = {"checkpoint": {"latest": {"resources": stelvio_env.export_resources()}}}
    manifest = read_network_manifest(state)

    assert len(manifest.vpcs) == 1
    vpc = manifest.vpcs[0]
    assert vpc.policy == BastionPolicy.PERSISTENT
    assert vpc.access is not None
    assert vpc.access.instance_id
    assert vpc.access.security_group_id
    assert vpc.access.owner == vpc.identity
    assert len(manifest.resources) == 1
    database = manifest.resources[0]
    assert database.service == "documentdb"
    assert database.vpc == vpc.identity
    assert database.resource_id == outputs["document_db_todos_cluster_id"]
    assert database.ports == (27017,)
    assert len(manifest.endpoints) == 1
    endpoint = manifest.endpoints[0]
    assert endpoint.vpcs == (vpc.identity,)
    assert manifest.enabled_vpcs == (vpc,)

    components = get_deployed_components(state)
    assert components
    assert all(not key.startswith("_") for item in components for key in item.outputs)

    caller_account = _boto3_session().client("sts").get_caller_identity()["Account"]
    assert caller_account == vpc.account

    assert_bastion_resources(
        vpc.access.instance_id,
        vpc_id=outputs["vpc_net_id"],
        public_subnet_ids=[subnet.subnet_id for subnet in vpc.public_subnets],
        security_group_id=vpc.access.security_group_id,
        identity_document=vpc.access.identity_document,
    )

    function_vpc = get_lambda_vpc_config(outputs["function_probe_arn"])
    assert function_vpc["VpcId"] == outputs["vpc_net_id"]
    assert set(function_vpc["SubnetIds"]) == set(outputs["vpc_net_private_subnet_ids"])
    assert len(function_vpc["SecurityGroupIds"]) == 1

    cluster_id = outputs["document_db_todos_cluster_id"]
    cluster = (
        _boto3_session()
        .client("docdb")
        .describe_db_clusters(DBClusterIdentifier=cluster_id)["DBClusters"][0]
    )
    assert outputs["document_db_todos_security_group_id"] in {
        group["VpcSecurityGroupId"] for group in cluster["VpcSecurityGroups"]
    }
    app_security_group_id = function_vpc["SecurityGroupIds"][0]
    db_group = get_security_group(outputs["document_db_todos_security_group_id"])
    assert db_group["VpcId"] == outputs["vpc_net_id"]
    assert_document_db_ingress_from_bastion(
        outputs["document_db_todos_security_group_id"],
        bastion_security_group_id=vpc.access.security_group_id,
        app_security_group_id=app_security_group_id,
        port=cluster["Port"],
    )
