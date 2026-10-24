import os
import platform
import shutil
import time
from pathlib import Path

import pytest

from .assert_helpers import _boto3_session
from .stelvio_test_env import StelvioTestEnv

# Shared customize dict to skip CloudFront edge propagation (10-20 min).
# Property tests only verify configuration, not edge availability.
NO_WAIT_DEPLOY = {"distribution": {"wait_for_deployment": False}}

# S3 buckets that receive objects during tests need force_destroy=True,
# otherwise Pulumi can't delete non-empty buckets and destroy fails.
FORCE_DESTROY_BUCKET = {"bucket": {"force_destroy": True}}


# Test tiers — each requires different env config or worker count. Tiers run as
# separate pytest processes in parallel; run_all.sh is the canonical runner and
# the single source of truth for test/worker counts.
#
#   integration          — standard tests, AWS profile only
#   integration_vpc      — tests that create a Vpc (4 workers max: account
#                          quota is 10 VPCs including the default VPC)
#   integration_cf       — CloudFront/Router/S3StaticWebsite, slow teardown
#   integration_dns      — needs STLV_TEST_DNS_DOMAIN + STLV_TEST_DNS_ZONE_ID
#                          (optional STLV_TEST_ACM_CERTIFICATE_ARN for a pre-issued
#                          wildcard cert; otherwise one is found/created per session)
#   integration_macos_privileged — exclusive host mutation tests; never run by run_all.sh
#   integration_tunnel    — AWS tunnel tests; also require the privileged macOS lane
#
# Run: STLV_TEST_AWS_PROFILE=<profile> tests/integration/run_all.sh
#
# Future tiers:
#   cloudflare — Cloudflare DNS tests (needs Cloudflare API token + zone, slow propagation)


def pytest_addoption(parser):
    parser.addoption(
        "--integration-linux-privileged",
        action="store_true",
        default=False,
        help="Exclusive Debian 13 native tests (use -n 0)",
    )
    parser.addoption(
        "--integration-macos-privileged",
        action="store_true",
        default=False,
        help="Exclusive macOS host mutation tests; never run by run_all.sh (use -n 0)",
    )
    parser.addoption(
        "--integration-tunnel",
        action="store_true",
        default=False,
        help="Run AWS-backed native VPC tunnel tests (also needs --integration-macos-privileged)",
    )
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that deploy real AWS resources",
    )
    parser.addoption(
        "--integration-vpc",
        action="store_true",
        default=False,
        help="Run VPC-creating integration tests (4 workers max; account VPC quota is 10)",
    )
    parser.addoption(
        "--integration-cf",
        action="store_true",
        default=False,
        help="Run CloudFront tier integration tests (slow teardown, use fewer workers)",
    )
    parser.addoption(
        "--integration-dns",
        action="store_true",
        default=False,
        help="Run DNS tier integration tests (needs STLV_TEST_DNS_DOMAIN + STLV_TEST_DNS_ZONE_ID)",
    )


def _validate_privileged_lane(config):
    run_tunnel = config.getoption("--integration-tunnel")
    run_linux = config.getoption("--integration-linux-privileged", default=False)
    run_host = config.getoption("--integration-macos-privileged") or run_linux
    if run_tunnel and not run_host:
        raise pytest.UsageError(
            "--integration-tunnel also requires --integration-macos-privileged"
        )
    if run_host:
        if config.getoption("numprocesses", default=0) or any(
            config.getoption(flag)
            for flag in (
                "--integration",
                "--integration-vpc",
                "--integration-cf",
                "--integration-dns",
            )
        ):
            raise pytest.UsageError(
                "The privileged macOS lane requires -n 0; only "
                "--integration-tunnel may be combined"
            )
        version = platform.mac_ver()[0].split(".")[0]
        if run_linux:
            from stelvio.tunnel.linux_platform import supported

            if platform.system() != "Linux" or not supported():
                raise pytest.UsageError(
                    "The Linux privileged lane requires Debian 13 and resolved"
                )
        elif platform.system() != "Darwin" or not version.isdigit() or int(version) < 15:
            raise pytest.UsageError("The privileged lane requires macOS 15+")
        if os.geteuid() == 0:
            raise pytest.UsageError(
                "Run pytest as an ordinary user; authorize administration separately"
            )
    return run_host, run_tunnel


def _mark_privileged_item(item, run_host, run_tunnel):
    linux_marker = item.get_closest_marker("integration_linux_privileged")
    if not item.get_closest_marker("integration_macos_privileged") and not linux_marker:
        return False
    if linux_marker and platform.system() != "Linux":
        item.add_marker(pytest.mark.skip(reason="Linux host test"))
        return True
    if (
        not linux_marker
        and platform.system() == "Linux"
        and not item.get_closest_marker("integration_tunnel")
    ):
        item.add_marker(pytest.mark.skip(reason="macOS native test; use the Linux host suite"))
        return True
    if not run_host:
        item.add_marker(pytest.mark.skip(reason="need --integration-macos-privileged"))
    elif item.get_closest_marker("integration_tunnel") and not run_tunnel:
        item.add_marker(pytest.mark.skip(reason="also need --integration-tunnel for AWS"))
    return True


def _mark_tier_item(config, item):
    # VPC tier takes precedence over the inherited/standard integration marker.
    for tier, option in (
        ("integration_vpc", "--integration-vpc"),
        ("integration_dns", "--integration-dns"),
        ("integration_cf", "--integration-cf"),
        ("integration", "--integration"),
    ):
        if item.get_closest_marker(tier):
            if not config.getoption(option):
                item.add_marker(pytest.mark.skip(reason=f"need {option} flag to run"))
            return


def _mark_integration_items(config, items, run_host, run_tunnel):
    for item in items:
        if _mark_privileged_item(item, run_host, run_tunnel):
            continue
        if item.get_closest_marker("integration_tunnel"):
            raise pytest.UsageError("Tunnel tests must also declare integration_macos_privileged")
        if run_host:
            item.add_marker(pytest.mark.skip(reason="exclusive privileged macOS lane"))
            continue
        _mark_tier_item(config, item)


def pytest_collection_modifyitems(config, items):
    run_host, run_tunnel = _validate_privileged_lane(config)
    _mark_integration_items(config, items, run_host, run_tunnel)


@pytest.fixture
def project_dir(tmp_path):
    """Set up a temp project directory with stlv_app.py and handler files.

    Needed for tests that deploy Functions (Function, Cron, RestApi).
    """
    from stelvio.project import get_project_root

    get_project_root.cache_clear()

    # Copy handler files into the temp project
    handlers_src = Path(__file__).parent / "handlers"
    handlers_dst = tmp_path / "handlers"
    shutil.copytree(handlers_src, handlers_dst)

    # Create dummy stlv_app.py so get_project_root() finds this dir
    (tmp_path / "stlv_app.py").touch()

    original_cwd = Path.cwd()
    os.chdir(tmp_path)

    yield tmp_path

    os.chdir(original_cwd)
    get_project_root.cache_clear()


@pytest.fixture
def stelvio_env(request):
    env = StelvioTestEnv(
        test_name=request.node.name,
        aws_profile=os.environ.get("STLV_TEST_AWS_PROFILE"),
        aws_region=os.environ.get("STLV_TEST_AWS_REGION", "us-east-1"),
    )
    try:
        yield env
    finally:
        env.destroy()


@pytest.fixture
def dns_domain():
    """Test domain from STLV_TEST_DNS_DOMAIN env var."""
    domain = os.environ.get("STLV_TEST_DNS_DOMAIN")
    if not domain:
        pytest.skip("STLV_TEST_DNS_DOMAIN not set")
    return domain


@pytest.fixture
def dns_zone_id():
    """Route 53 zone ID from STLV_TEST_DNS_ZONE_ID env var."""
    zone_id = os.environ.get("STLV_TEST_DNS_ZONE_ID")
    if not zone_id:
        pytest.skip("STLV_TEST_DNS_ZONE_ID not set")
    return zone_id


# ACM can take ~1 minute to expose the DNS validation CNAME.
_ACM_VALIDATION_RECORD_ATTEMPTS = 30
_ACM_VALIDATION_RECORD_INTERVAL = 2


def _find_wildcard_cert(acm, wildcard: str) -> dict | None:
    """Return an ISSUED cert for the wildcard, else a PENDING_VALIDATION one."""
    pending = None
    paginator = acm.get_paginator("list_certificates")
    for page in paginator.paginate(CertificateStatuses=["ISSUED", "PENDING_VALIDATION"]):
        for summary in page["CertificateSummaryList"]:
            if summary.get("DomainName") != wildcard:
                continue
            if summary.get("Status") == "ISSUED":
                return summary
            pending = summary
    return pending


def _ensure_wildcard_certificate_arn(domain: str, zone_id: str) -> str:
    """Return an ISSUED ACM cert for *.{domain}, creating one if needed.

    Left in the account for reuse across runs (tagged stelvio:env=test only,
    no stelvio:app, so cleanup skips them).
    """
    session = _boto3_session()
    acm = session.client("acm")
    route53 = session.client("route53")
    wildcard = f"*.{domain}"

    existing = _find_wildcard_cert(acm, wildcard)
    if existing and existing.get("Status") == "ISSUED":
        return existing["CertificateArn"]

    arn = (
        existing["CertificateArn"]
        if existing
        else acm.request_certificate(
            DomainName=wildcard,
            ValidationMethod="DNS",
            Tags=[
                {"Key": "stelvio:env", "Value": "test"},
                {"Key": "stelvio:purpose", "Value": "integration-dns-wildcard"},
            ],
        )["CertificateArn"]
    )

    record = None
    for _ in range(_ACM_VALIDATION_RECORD_ATTEMPTS):
        options = acm.describe_certificate(CertificateArn=arn)["Certificate"].get(
            "DomainValidationOptions", []
        )
        if options and options[0].get("ResourceRecord"):
            record = options[0]["ResourceRecord"]
            break
        time.sleep(_ACM_VALIDATION_RECORD_INTERVAL)
    if record is None:
        raise RuntimeError(f"ACM validation record not available for {arn}")

    route53.change_resource_record_sets(
        HostedZoneId=zone_id,
        ChangeBatch={
            "Changes": [
                {
                    "Action": "UPSERT",
                    "ResourceRecordSet": {
                        "Name": record["Name"],
                        "Type": record["Type"],
                        "TTL": 60,
                        "ResourceRecords": [{"Value": record["Value"]}],
                    },
                }
            ]
        },
    )
    acm.get_waiter("certificate_validated").wait(
        CertificateArn=arn,
        WaiterConfig={"Delay": 5, "MaxAttempts": 60},
    )
    return arn


@pytest.fixture(scope="session")
def dns_certificate_arn():
    """ISSUED ACM certificate ARN covering *.{STLV_TEST_DNS_DOMAIN}.

    Prefer STLV_TEST_ACM_CERTIFICATE_ARN when set; otherwise find or create a
    wildcard cert in the test account (reused across runs).

    Session scope is per xdist worker (run_all.sh uses -n 3 for dns), so two
    dns tests on different workers would both request_certificate. Only one
    test uses this fixture now.
    """
    explicit = os.environ.get("STLV_TEST_ACM_CERTIFICATE_ARN")
    if explicit:
        return explicit

    domain = os.environ.get("STLV_TEST_DNS_DOMAIN")
    zone_id = os.environ.get("STLV_TEST_DNS_ZONE_ID")
    if not domain or not zone_id:
        pytest.skip("STLV_TEST_DNS_DOMAIN and STLV_TEST_DNS_ZONE_ID required")
    return _ensure_wildcard_certificate_arn(domain, zone_id)


@pytest.fixture(scope="session")
def macos_host(tmp_path_factory):
    """Preserve and restore the host before exclusive native acceptance."""
    if platform.system() == "Linux":
        from .linux_host import LinuxHost as Host
    else:
        from .macos_host import MacOSHost as Host

    host = Host(tmp_path_factory.mktemp("macos-host"))
    try:
        host.prepare()
        yield host
    finally:
        try:
            host.restore()
        finally:
            host.close()


@pytest.fixture(autouse=True)
def _privileged_host_baseline(request):
    if not (
        request.node.get_closest_marker("integration_macos_privileged")
        or request.node.get_closest_marker("integration_linux_privileged")
    ):
        yield
        return
    host = request.getfixturevalue("macos_host")
    host.assert_restored("before-" + request.node.name)
    try:
        yield
    finally:
        host.assert_restored("after-" + request.node.name)
