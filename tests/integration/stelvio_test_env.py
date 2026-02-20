import os
import re
import shutil
import tempfile
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path

from pulumi.automation import (
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    PulumiCommand,
    Stack,
    create_or_select_stack,
)
from semver import VersionInfo

from stelvio.app import StelvioApp
from stelvio.aws.api_gateway.iam import _create_api_gateway_account_and_role
from stelvio.aws.function.function import LinkPropertiesRegistry
from stelvio.component import ComponentRegistry
from stelvio.config import AwsConfig, StelvioAppConfig
from stelvio.context import AppContext, _ContextStore
from stelvio.dns import Dns
from stelvio.pulumi import get_stelvio_config_dir

_pulumi_command: PulumiCommand | None = None


def _get_pulumi_command() -> PulumiCommand:
    """Get or install Pulumi CLI at the version pinned by the Stelvio SDK."""
    global _pulumi_command  # noqa: PLW0603
    if _pulumi_command is None:
        _pulumi_command = PulumiCommand.install(
            root=str(get_stelvio_config_dir()),
            version=VersionInfo.parse(version("pulumi")),
        )
    return _pulumi_command


class StelvioTestEnv:
    """Test environment for deploying real Stelvio components to AWS."""

    def __init__(
        self,
        test_name: str,
        aws_profile: str | None = None,
        aws_region: str = "us-east-1",
    ):
        self._run_id = os.urandom(3).hex()  # 6 hex chars for uniqueness
        sanitized = re.sub(r"[^a-zA-Z0-9-]", "-", test_name)
        self._stack_name = f"integ-{sanitized}"[:50]
        self._aws_profile = aws_profile
        self._aws_region = aws_region
        self._stack: Stack | None = None
        self._workdir: Path | None = None

    def deploy(self, infra_fn: Callable[[], None], *, dns: Dns | None = None) -> dict[str, str]:
        """Deploy components defined in infra_fn. Returns outputs as plain dict."""
        self._reset_singletons()

        app = StelvioApp(f"stlv-{self._run_id}")

        @app.config
        def config(env):
            return StelvioAppConfig(
                aws=AwsConfig(profile=self._aws_profile, region=self._aws_region),
                dns=dns,
            )

        @app.run
        def run():
            infra_fn()

        return self._deploy_stack(app)

    def deploy_app(self, app: StelvioApp) -> dict[str, str]:
        """Deploy a user-provided StelvioApp. Returns outputs as plain dict.

        Use this instead of deploy() when the test needs custom app configuration
        that deploy()'s standard setup doesn't support — e.g. global customize,
        default link overrides via set_user_link_for(), or module loading.
        """
        # Clear only _ContextStore (set by autouse fixture in tests/conftest.py).
        # Cannot use _reset_singletons() because the caller has already registered
        # link_configs in ComponentRegistry._user_link_creators via StelvioApp().
        _ContextStore.clear()
        return self._deploy_stack(app)

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def aws_profile(self) -> str | None:
        return self._aws_profile

    @property
    def aws_region(self) -> str:
        return self._aws_region

    def _deploy_stack(self, app: StelvioApp) -> dict[str, str]:
        if self._stack is not None:
            raise RuntimeError(
                "deploy() or deploy_app() called twice on the same StelvioTestEnv. "
                "Each test should call deploy exactly once."
            )
        self._workdir = Path(tempfile.mkdtemp(prefix="stelvio-test-"))

        stelvio_config = app._execute_user_config_func("test")
        _ContextStore.set(
            AppContext(
                name=app._name,
                env="test",
                aws=stelvio_config.aws,
                home=stelvio_config.home,
                dns=stelvio_config.dns,
                customize=stelvio_config.customize,
            )
        )

        passphrase = "test-passphrase-not-secret"  # noqa: S105
        backend = ProjectBackend(f"file://{self._workdir}")
        project_settings = ProjectSettings(name=app._name, runtime="python", backend=backend)

        env_vars = {"PULUMI_CONFIG_PASSPHRASE": passphrase}
        if self._aws_region:
            env_vars["AWS_REGION"] = self._aws_region
        if self._aws_profile:
            env_vars["AWS_PROFILE"] = self._aws_profile

        pulumi_cmd = _get_pulumi_command()

        opts = LocalWorkspaceOptions(
            pulumi_command=pulumi_cmd,
            env_vars=env_vars,
            project_settings=project_settings,
            pulumi_home=str(get_stelvio_config_dir() / ".pulumi"),
        )

        self._stack = create_or_select_stack(
            stack_name=self._stack_name,
            project_name=app._name,
            program=app._get_pulumi_program_func(),
            opts=opts,
        )
        result = self._stack.up(on_output=print)
        return {k: v.value for k, v in result.outputs.items()}

    def destroy(self) -> None:
        """Destroy all resources and clean up. Always safe to call."""
        if not self._stack:
            if self._workdir and self._workdir.exists():
                shutil.rmtree(self._workdir, ignore_errors=True)
            self._reset_singletons()
            return

        try:
            self._stack.destroy(on_output=print)
        except Exception as first_err:
            # State might be out of sync — refresh and retry
            try:
                self._stack.refresh(on_output=print)
                self._stack.destroy(on_output=print)
            except Exception as retry_err:
                # Cannot destroy — keep workdir for manual cleanup
                print(f"CRITICAL: Could not destroy stack '{self._stack_name}'")
                print(f"First error: {first_err}")
                print(f"Retry error: {retry_err}")
                print(f"State dir: {self._workdir}")
                print("Manual cleanup required. Resources have 'integ-' prefix.")
                self._reset_singletons()
                return

        # Destroy succeeded — clean up state
        if self._workdir and self._workdir.exists():
            shutil.rmtree(self._workdir, ignore_errors=True)
        self._reset_singletons()

    def _reset_singletons(self) -> None:
        """Reset all Stelvio global state between tests.

        Note: With pytest-xdist each test runs in a separate process, so this
        is only needed when tests run sequentially in the same process. If
        sequential runs hang due to lingering gRPC state, consider adding
        pulumi.runtime.reset_options() and gc.collect() here.
        """
        ComponentRegistry._instances.clear()
        ComponentRegistry._registered_names.clear()
        ComponentRegistry._user_link_creators.clear()
        LinkPropertiesRegistry._folder_links_properties_map.clear()
        _create_api_gateway_account_and_role.cache_clear()
        _ContextStore.clear()
        StelvioApp._StelvioApp__instance = None  # type: ignore[attr-defined]
