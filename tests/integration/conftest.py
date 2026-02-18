import os
import shutil
from pathlib import Path

import pytest

from .stelvio_test_env import StelvioTestEnv

# Shared customize dict to skip CloudFront edge propagation (10-20 min).
# Property tests only verify configuration, not edge availability.
NO_WAIT_DEPLOY = {"distribution": {"wait_for_deployment": False}}


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that deploy real AWS resources",
    )
    parser.addoption(
        "--integration-dns",
        action="store_true",
        default=False,
        help="Run DNS tier integration tests (needs STLV_TEST_DNS_DOMAIN + STLV_TEST_DNS_ZONE_ID)",
    )


def pytest_collection_modifyitems(config, items):
    run_integration = config.getoption("--integration")
    run_dns = config.getoption("--integration-dns")

    skip_integration = pytest.mark.skip(reason="need --integration flag to run")
    skip_dns = pytest.mark.skip(reason="need --integration-dns flag to run")

    for item in items:
        if "integration_dns" in item.keywords:
            if not run_dns:
                item.add_marker(skip_dns)
        elif "integration" in item.keywords and not run_integration:
            item.add_marker(skip_integration)


@pytest.fixture
def project_dir(tmp_path):
    """Set up a temp project directory with stlv_app.py and handler files.

    Needed for tests that deploy Functions (Function, Cron, Api).
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
    yield env
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
