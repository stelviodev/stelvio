"""Boto3 checks for bastion connectivity and DocumentDB access."""

import time

from .assert_helpers import _boto3_session
from .assert_vpc import get_security_group

# Allow the agent ten minutes to register after initial boot and IAM propagation.
_SSM_ONLINE_TIMEOUT = 600
_SSM_ONLINE_INTERVAL = 10


def assert_bastion_online(instance_id: str) -> None:
    """Wait for the deployed image, IAM permissions and network path to reach SSM."""
    ssm = _boto3_session().client("ssm")
    deadline = time.monotonic() + _SSM_ONLINE_TIMEOUT
    status = "not registered"
    while time.monotonic() < deadline:
        instances = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )["InstanceInformationList"]
        status = instances[0]["PingStatus"] if instances else "not registered"
        if status == "Online":
            return
        time.sleep(_SSM_ONLINE_INTERVAL)
    raise AssertionError(
        f"Bastion '{instance_id}' did not become Online in SSM within "
        f"{_SSM_ONLINE_TIMEOUT}s; last status: {status}. Check the SSM Agent logs, "
        "instance IAM role and outbound connectivity to the SSM endpoints."
    )


def assert_document_db_ingress_from_bastion(
    security_group_id: str,
    *,
    bastion_security_group_id: str,
    app_security_group_id: str,
    port: int,
) -> None:
    """Assert DocumentDB admits only its app and bastion groups on the service port."""
    group = get_security_group(security_group_id)
    actual_sources = set()
    for permission in group["IpPermissions"]:
        assert permission["IpProtocol"] == "tcp"
        assert permission["FromPort"] == port
        assert permission["ToPort"] == port
        assert permission.get("IpRanges", []) == []
        assert permission.get("Ipv6Ranges", []) == []
        actual_sources.update(pair["GroupId"] for pair in permission.get("UserIdGroupPairs", []))
    assert actual_sources == {bastion_security_group_id, app_security_group_id}
