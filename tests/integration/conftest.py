import os
import shutil
from pathlib import Path

import pytest

from .stelvio_test_env import StelvioTestEnv

# Shared customize dict to skip CloudFront edge propagation (10-20 min).
# Property tests only verify configuration, not edge availability.
NO_WAIT_DEPLOY = {"distribution": {"wait_for_deployment": False}}

# S3 buckets that receive objects during tests need force_destroy=True,
# otherwise Pulumi can't delete non-empty buckets and destroy fails.
FORCE_DESTROY_BUCKET = {"bucket": {"force_destroy": True}}


# Test tiers — each requires different env config or worker count.
# Tiers run as separate pytest processes in parallel (see run_all.sh).
# Worker counts are chosen so tests divide evenly with no straggler.
# Adjust worker counts in run_all.sh when adding/removing tests.
#
#   integration     — standard tests, AWS profile only (122 tests / 10 workers)
#     STELVIO_TEST_AWS_PROFILE=<profile> uv run pytest tests/integration/ --integration -v -n 10
#
#   integration_cf  — CloudFront/Router/S3StaticWebsite, slow teardown (13 tests / 7 workers)
#     STELVIO_TEST_AWS_PROFILE=<profile> uv run pytest tests/integration/ --integration-cf -v -n 7
#
#   integration_dns — needs Route 53 domain + zone ID (7 tests / 4 workers)
#     STELVIO_TEST_AWS_PROFILE=<profile> STELVIO_TEST_DNS_DOMAIN=<domain> \
#       STELVIO_TEST_DNS_ZONE_ID=<zone-id> \
#       uv run pytest tests/integration/ --integration-dns -v -n 4
#
# Run all tiers in parallel:
#     tests/integration/run_all.sh
#
# Future tiers:
#   cloudflare — Cloudflare DNS tests (needs Cloudflare API token + zone, slow propagation)


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that deploy real AWS resources",
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
        help="Run DNS tier integration tests "
        "(needs STELVIO_TEST_DNS_DOMAIN + STELVIO_TEST_DNS_ZONE_ID)",
    )


def pytest_collection_modifyitems(config, items):
    run_integration = config.getoption("--integration")
    run_cf = config.getoption("--integration-cf")
    run_dns = config.getoption("--integration-dns")

    skip_integration = pytest.mark.skip(reason="need --integration flag to run")
    skip_cf = pytest.mark.skip(reason="need --integration-cf flag to run")
    skip_dns = pytest.mark.skip(reason="need --integration-dns flag to run")

    for item in items:
        if item.get_closest_marker("integration_dns"):
            if not run_dns:
                item.add_marker(skip_dns)
        elif item.get_closest_marker("integration_cf"):
            if not run_cf:
                item.add_marker(skip_cf)
        elif item.get_closest_marker("integration") and not run_integration:
            item.add_marker(skip_integration)


@pytest.fixture
def project_dir(tmp_path):
    """Set up a temp project directory with stelvio_app.py and handler files.

    Needed for tests that deploy Functions (Function, Cron, Api).
    """
    from stelvio.project import get_project_root

    get_project_root.cache_clear()

    # Copy handler files into the temp project
    handlers_src = Path(__file__).parent / "handlers"
    handlers_dst = tmp_path / "handlers"
    shutil.copytree(handlers_src, handlers_dst)

    # Create dummy stelvio_app.py so get_project_root() finds this dir
    (tmp_path / "stelvio_app.py").touch()

    original_cwd = Path.cwd()
    os.chdir(tmp_path)

    yield tmp_path

    os.chdir(original_cwd)
    get_project_root.cache_clear()


@pytest.fixture
def stelvio_env(request):
    env = StelvioTestEnv(
        test_name=request.node.name,
        aws_profile=os.environ.get("STELVIO_TEST_AWS_PROFILE"),
        aws_region=os.environ.get("STELVIO_TEST_AWS_REGION", "us-east-1"),
    )
    yield env
    env.destroy()


@pytest.fixture
def dns_domain():
    """Test domain from STELVIO_TEST_DNS_DOMAIN env var."""
    domain = os.environ.get("STELVIO_TEST_DNS_DOMAIN")
    if not domain:
        pytest.skip("STELVIO_TEST_DNS_DOMAIN not set")
    return domain


@pytest.fixture
def dns_zone_id():
    """Route 53 zone ID from STELVIO_TEST_DNS_ZONE_ID env var."""
    zone_id = os.environ.get("STELVIO_TEST_DNS_ZONE_ID")
    if not zone_id:
        pytest.skip("STELVIO_TEST_DNS_ZONE_ID not set")
    return zone_id
