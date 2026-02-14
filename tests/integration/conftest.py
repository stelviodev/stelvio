import os

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
def stelvio_env(request):
    env = StelvioTestEnv(
        test_name=request.node.name,
        aws_profile=os.environ.get("STLV_TEST_AWS_PROFILE"),
        aws_region=os.environ.get("STLV_TEST_AWS_REGION", "us-east-1"),
    )
    yield env
    env.destroy()
