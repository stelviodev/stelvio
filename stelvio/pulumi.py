import getpass
import logging
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from importlib import import_module
from io import BytesIO
from pathlib import Path

import requests
from appdirs import user_config_dir
from pulumi.automation import (
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    PulumiCommand,
    Stack,
    create_or_select_stack,
    fully_qualified_stack_name,
)
from rich.console import Console
from semver import VersionInfo

from stelvio.app import StelvioApp
from stelvio.aws.function.dependencies import (
    clean_function_active_dependencies_caches_file,
    clean_function_stale_dependency_caches,
)
from stelvio.aws.layer import (
    clean_layer_active_dependencies_caches_file,
    clean_layer_stale_dependency_caches,
)
from stelvio.exceptions import StelvioProjectError
from stelvio.passphrase import get_passphrase
from stelvio.project import get_last_deployed_app_name, get_project_root, save_deployed_app_name

logger = logging.getLogger(__name__)

PULUMI_VERSION = "v3.170.0"


def get_stelvio_config_dir() -> Path:
    return Path(user_config_dir(appname="stelvio"))


def get_bin_path() -> Path:
    stelvio_bin_path = get_stelvio_config_dir() / "bin"
    stelvio_bin_path.mkdir(parents=True, exist_ok=True)
    return stelvio_bin_path


def pulumi_path() -> Path:
    executable_name = "pulumi.exe" if sys.platform == "win32" else "pulumi"
    return get_bin_path() / executable_name


def load_stlv_app() -> None:
    logger.debug("CWD %s", Path.cwd())
    logger.debug("SYS PATH %s", sys.path)

    original_sys_path = list(sys.path)
    try:
        project_root = get_project_root()
    except ValueError as e:
        logger.exception("Failed to find Stelvio project: %s")
        raise StelvioProjectError(
            "No Stelvio project found. Run 'stlv init' to create a new project in this directory."
        ) from e

    logger.debug("PROJECT ROOT: %s", project_root)
    if project_root not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        import_module("stlv_app")
    finally:
        sys.path = original_sys_path


def run_pulumi_preview(environment: str | None) -> None:
    load_stlv_app()
    stack = prepare_pulumi_stack(environment)

    # Clean active cache tracking files at the start of the run
    clean_function_active_dependencies_caches_file()
    clean_layer_active_dependencies_caches_file()

    logger.info("Previewing changes for %s ...", environment)
    stack.preview(on_output=print, color="always")
    clean_function_stale_dependency_caches()
    clean_layer_stale_dependency_caches()


def run_pulumi_deploy(environment: str | None, confirmed_new_app: bool = False) -> None:
    load_stlv_app()

    # Check for app rename
    app = StelvioApp.get_instance()
    current_app_name = app._name  # noqa: SLF001
    last_deployed_name = get_last_deployed_app_name()

    if last_deployed_name and last_deployed_name != current_app_name and not confirmed_new_app:
        from stelvio.exceptions import AppRenamedError

        raise AppRenamedError(last_deployed_name, current_app_name)

    stack = prepare_pulumi_stack(environment)

    clean_function_active_dependencies_caches_file()
    clean_layer_active_dependencies_caches_file()

    logger.info("Deploying %s ...", environment)
    stack.up(on_output=print, color="always")
    clean_function_stale_dependency_caches()
    clean_layer_stale_dependency_caches()

    # Save the app name after successful deployment
    save_deployed_app_name(current_app_name)


def run_pulumi_refresh(environment: str | None) -> None:
    load_stlv_app()
    stack = prepare_pulumi_stack(environment)

    logger.info("Refreshing environment for %s ...", environment)
    console = Console()
    with console.status("Refreshing state from AWS..."):
        result = stack.refresh(on_output=print)

    # Check for actual drift (not just 'same' resources)
    changes = result.summary.resource_changes
    drift_detected = any(key != "same" and count > 0 for key, count in changes.items())
    if drift_detected:
        console.print("⚠️ Drift detected:")
        for change_type, count in changes.items():
            if change_type != "same" and count > 0:
                console.print(f"  {count} resources {change_type}d")
        console.print("\n💡 Next steps:")
        console.print("  • Run 'stlv diff' to see what your code would change")
        console.print("  • Update your code to match current AWS state, or")
        console.print("  • Run 'stlv deploy' to revert AWS to your code")
    else:
        total_resources = changes.get("same", 0)
        console.print(f"✅ State refreshed - {total_resources} resources in sync")


def run_pulumi_destroy(environment: str | None) -> None:
    load_stlv_app()
    stack = prepare_pulumi_stack(environment)

    logger.info("Destroying for %s ...", environment)
    stack.destroy(on_output=print, color="always")
    logger.info("Environment %s destroyed", environment)
    stack.workspace.remove_stack(environment)


def prepare_pulumi_stack(environment: str) -> Stack:
    app = StelvioApp.get_instance()
    project_name = app._name  # noqa: SLF001
    logger.debug("Getting project configuration for environment: %s", environment)
    config = app._execute_user_config_func(environment)  # noqa: SLF001

    passphrase = get_passphrase(project_name, environment, config.aws.profile, config.aws.region)

    # Validate environment
    username = getpass.getuser()
    if not config.is_valid_environment(environment, username):
        raise ValueError(
            f"Invalid environment '{environment}'. Use your username '{username}' for personal "
            f"environments or one of: {config.environments}"
        )

    stack_name = fully_qualified_stack_name("organization", project_name, environment)
    logger.debug("Fully qualified stack name: %s", stack_name)

    # Pulumi creates its yaml files in tmp dir

    # We store state outside of main ~/.pulumi, instead in .pulumi folder in config dir - so we can
    # clean up workspaces json files. but we could do it also if they're in ~/.pulumi by using
    # project name
    state_dir_path = get_stelvio_config_dir()
    backend = ProjectBackend(f"file://{state_dir_path}")
    project_settings = ProjectSettings(name=project_name, runtime="python", backend=backend)
    logger.debug("Setting up workspace")
    opts = LocalWorkspaceOptions(
        pulumi_command=PulumiCommand(str(get_stelvio_config_dir()), VersionInfo(3, 170, 0)),
        env_vars={
            "PULUMI_CONFIG_PASSPHRASE": passphrase,
            "AWS_PROFILE": config.aws.profile,
            "AWS_REGION": config.aws.region,
        },
        project_settings=project_settings,
        # pulumi_home if set is where pulumi installs plugins; otherwise it goes to ~/.pulumi
        # pulumi_home=str(get_stelvio_config_dir() / ".pulumi"),
    )
    logger.debug("Creating stack")
    stack = create_or_select_stack(
        stack_name=stack_name,
        project_name=project_name,
        program=app._get_pulumi_program_func(),  # noqa: SLF001
        opts=opts,
    )
    logger.debug("Successfully initialized stack")

    return stack


def needs_pulumi() -> bool:
    pulumi_exe_path = pulumi_path()
    if not pulumi_exe_path.exists():
        return True
    try:
        process = subprocess.run(  # noqa: S603
            [str(pulumi_exe_path), "version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        return process.returncode != 0 or process.stdout.strip() != PULUMI_VERSION
    except (subprocess.TimeoutExpired, FileNotFoundError, Exception):
        return True


def install_pulumi() -> None:
    os_map = {"linux": "linux", "darwin": "darwin", "win32": "windows"}
    arch_map = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}

    current_os, current_arch = sys.platform, platform.machine().lower()

    if current_os not in os_map or current_arch not in arch_map:
        raise RuntimeError(f"Unsupported OS/Arch: {current_os}/{current_arch}")

    pulumi_os, pulumi_arch = os_map[current_os], arch_map[current_arch]
    archive_ext = ".zip" if pulumi_os == "windows" else ".tar.gz"
    url = (
        f"https://github.com/pulumi/pulumi/releases/download/{PULUMI_VERSION}"
        f"/pulumi-{PULUMI_VERSION}-{pulumi_os}-{pulumi_arch}{archive_ext}"
    )

    logger.info("Downloading Pulumi from %s", url)

    tmp_path = get_bin_path() / "pulumi_tmp"
    if tmp_path.exists():
        shutil.rmtree(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)

    try:
        with requests.get(url, timeout=600) as r:
            r.raise_for_status()
            logger.info("Extracting Pulumi to  %s", tmp_path)
            if archive_ext == ".tar.gz":
                with tarfile.open(fileobj=BytesIO(r.content), mode="r:gz") as tar:
                    tar.extractall(tmp_path, filter="data")
            elif archive_ext == ".zip":
                with zipfile.ZipFile(BytesIO(r.content), "r") as zip_ref:
                    zip_ref.extractall(tmp_path)  # noqa: S202

        move_pulumi_to_bin(pulumi_os, tmp_path)
        logger.info("Pulumi installed to  %s", get_bin_path())
    except requests.exceptions.RequestException:
        logger.exception("Failed to download Pulumi.")
    except (tarfile.TarError, zipfile.BadZipFile):
        logger.exception("Failed to extract Pulumi archive.")
    except Exception:
        logger.exception("An unexpected error occurred during Pulumi installation.")
    finally:
        if tmp_path.exists():
            shutil.rmtree(tmp_path, ignore_errors=True)


def move_pulumi_to_bin(pulumi_os: str, tmp_path: Path) -> None:
    dir_to_copy = tmp_path / "pulumi"
    if pulumi_os == "windows":
        dir_to_copy /= "bin"
    for item in dir_to_copy.iterdir():
        destination_path = get_bin_path() / item.name
        if destination_path.exists():
            if item.is_file():
                destination_path.unlink()
            elif item.is_dir():
                shutil.rmtree(destination_path)
        shutil.move(str(item), str(destination_path))
