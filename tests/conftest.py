import asyncio
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from pulumi.runtime import reset_options, set_mocks

from stelvio.aws.function.function import LinkPropertiesRegistry
from stelvio.command_run import _PRELOADED_APP_CONFIGS
from stelvio.component import ComponentRegistry
from stelvio.config import AwsConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.provider import ProviderStore

# Rewrite asserts in pulumi_mocks (assert_res & co.) so failures show pytest's full diff.
# Must run before the module is imported anywhere.
pytest.register_assert_rewrite("tests.aws.pulumi_mocks")

# TP imported only for re-export (F401): tests do `from conftest import TP`
from tests.aws.pulumi_mocks import TP, PulumiTestMocks  # noqa: E402, F401


def delete_files(directory: Path, filename: str):
    """Helper to clean up generated files."""
    for file_path in directory.rglob(filename):
        file_path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _event_loop():
    """A fresh event loop per test, closed after it.

    Constructing a Component registers a pulumi.ComponentResource, which queues async work
    on the current loop. A test that never pumps the loop (validation-only, no mocks) would
    otherwise hand that work to the next test that does. Cancel-then-gather is asyncio.run's
    shutdown step. Python 3.14 no longer creates a loop implicitly, so this also covers that.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield
    pending = asyncio.all_tasks(loop)
    for task in pending:
        task.cancel()
    if pending:  # gather() with no tasks would bind to whatever loop is current
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
    loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def clean_registries():
    LinkPropertiesRegistry._folder_links_properties_map.clear()
    ComponentRegistry._instances.clear()
    ComponentRegistry._registered_names.clear()
    ComponentRegistry._user_link_creators.clear()
    ProviderStore.reset()
    _PRELOADED_APP_CONFIGS.clear()


def mock_get_or_install_dependencies(path: str):
    with patch(path) as mock_ensure:
        # Simulate get_or_install_dependencies returning a valid cache path
        # Use a unique path per test potentially, or ensure cleanup
        mock_cache_path = Path("mock_cache_dir_for_fixture").resolve()
        mock_cache_path.mkdir(parents=True, exist_ok=True)
        # Add a dummy file to simulate non-empty cache after install
        (mock_cache_path / "dummy_installed_package").touch()
        mock_ensure.return_value = mock_cache_path
        yield mock_ensure
        # Clean up the dummy cache dir after test
        shutil.rmtree(mock_cache_path, ignore_errors=True)


@pytest.fixture
def mock_get_or_install_dependencies_layer():
    yield from mock_get_or_install_dependencies("stelvio.aws.layer.get_or_install_dependencies")


@pytest.fixture
def mock_get_or_install_dependencies_function():
    yield from mock_get_or_install_dependencies(
        "stelvio.aws.function.dependencies.get_or_install_dependencies"
    )


@pytest.fixture(autouse=True)
def app_context():
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(
            name="test",
            env="test",
            aws=AwsConfig(profile="default", region="us-east-1"),
            home="aws",
            customize={},
        )
    )


@pytest.fixture
def hermetic_aws(monkeypatch, tmp_path):
    """Nothing from the developer's machine reaches botocore: no profile, no static keys,
    ~/.aws files redirected to nonexistent paths, HOME moved (botocore reads the SSO token
    cache from ~/.aws/sso with no env override), instance metadata off. No mocking —
    the real credential and region chains run and find nothing.
    """
    for var in (
        "AWS_DEFAULT_PROFILE",
        "AWS_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("HOME", str(tmp_path))


@pytest.fixture(autouse=True)
def _hermetic_unless_integration(request):
    """Unit tests never see the machine's ~/.aws; tests/integration needs it."""
    if not request.path.is_relative_to(request.config.rootpath / "tests" / "integration"):
        request.getfixturevalue("hermetic_aws")


@pytest.fixture
def no_region_context(app_context, hermetic_aws, monkeypatch):
    """Context with NO region configured in Stelvio; the AWS chain resolves eu-central-1.

    Depends on `app_context` so it explicitly runs after (and replaces) the autouse
    default context rather than relying on fixture instantiation order.

    Reproduces the default new-user setup (bare AwsConfig()) that every hardcoded
    region="us-east-1" test context hides. The real boto3 chain picks the region up
    from AWS_REGION, pinned here for determinism; `hermetic_aws` keeps the machine's
    own setup out.
    """
    _ContextStore.clear()
    _ContextStore.set(
        AppContext(name="test", env="test", aws=AwsConfig(), home="aws", customize={})
    )
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


@pytest.fixture
def pulumi_mocks():
    mocks = PulumiTestMocks()
    set_mocks(mocks)
    yield mocks
    # set_mocks is process-global and Pulumi has no unset. This drops the monitor and the
    # root stack, so the next test never sees this test's mocks (the #264 flake class).
    reset_options()


@pytest.fixture
def project_cwd(monkeypatch, pytestconfig, tmp_path):
    """Provide a temporary Stelvio project root and chdir into it.

    This matches what Function-related tests need and can be reused
    by other components (e.g. CloudFront router) that instantiate
    real Functions with file-based handlers.
    """
    from stelvio.project import get_project_root

    # Ensure no stale cache from previous tests
    get_project_root.cache_clear()

    # Use the existing sample project as a realistic template
    rootpath = pytestconfig.rootpath
    source_project_dir = rootpath / "tests" / "aws" / "sample_test_project"
    temp_project_dir = tmp_path / "sample_project_copy"

    shutil.copytree(source_project_dir, temp_project_dir, dirs_exist_ok=True)

    # Remember original cwd and switch into the temp project
    original_cwd = Path.cwd()
    monkeypatch.chdir(temp_project_dir)

    yield temp_project_dir

    # Cleanup generated files and restore state
    delete_files(temp_project_dir, "stlv_resources.py")
    monkeypatch.chdir(original_cwd)
    get_project_root.cache_clear()
