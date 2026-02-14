import os
import shutil
from pathlib import Path

import pytest

from .stelvio_test_env import StelvioTestEnv


def pytest_addoption(parser):
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Run integration tests that deploy real AWS resources",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--integration"):
        return
    skip = pytest.mark.skip(reason="need --integration flag to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)


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
