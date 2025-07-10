import getpass
import logging
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from importlib import import_module
from io import BytesIO
from pathlib import Path
from typing import Literal, Optional

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
from pulumi.automation.errors import CommandError
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
from stelvio.context import AppContext, _ContextStore
from stelvio.exceptions import StelvioProjectError
from stelvio.passphrase import get_passphrase
from stelvio.project import get_last_deployed_app_name, get_project_root, save_deployed_app_name
from stelvio.rich_deployment_handler import RichDeploymentHandler

logger = logging.getLogger(__name__)
console = Console(soft_wrap=True)

PULUMI_VERSION = "v3.170.0"


def _should_skip_diagnostic(message: str) -> bool:
    return message.startswith("update failed") or "failed to register new resource" in message


def _is_exception_line(line: str) -> bool:
    return bool(re.match(r"^[A-Z][a-zA-Z0-9]*(?:Error|Exception|Warning)?\s*:", line))


def _is_traceback_line(line: str) -> bool:
    """Check if a line is part of a Python traceback."""
    stripped = line.strip()
    # Check if it's a traceback header or file reference
    if stripped.startswith(("Traceback", 'File "')):
        return True
    # Check if it's indented code (starts with spaces)
    return len(line) > 0 and line[0] == " "


def _extract_last_exception_line(lines: list[str]) -> str | None:
    """Extract the last line that looks like a Python exception."""
    for original_line in reversed(lines):
        line = original_line.strip()
        if _is_exception_line(line):
            return line
    return None


def _extract_last_non_traceback_line(lines: list[str]) -> str | None:
    """Extract the last non-empty line that's not part of a traceback."""
    for original_line in reversed(lines):
        line = original_line.strip()
        if line and not _is_traceback_line(line):
            return line
    return None


def _remove_pulumi_noise(message: str) -> str:
    """Remove Pulumi-specific noise from error message."""
    message = re.sub(r"<ref \*\d+>\s*", "", message)
    message = re.sub(r"(?m)^Running program .*$\n?", "", message)
    return message.strip()


def _parse_python_error(message: str) -> str:
    """Extract clean error message from Python traceback."""
    message = _remove_pulumi_noise(message)
    lines = message.strip().split("\n")

    # Try to find the actual exception line first
    exception_line = _extract_last_exception_line(lines)
    if exception_line:
        return exception_line

    # Fallback to last non-empty, non-traceback line
    fallback_line = _extract_last_non_traceback_line(lines)
    if fallback_line:
        return fallback_line

    # Final fallback - return the cleaned message as-is
    return message


def _show_simple_error(e: CommandError, handler: "RichDeploymentHandler") -> None:
    console.print("\n[bold red]| Error[/bold red]\n")

    if handler.error_diagnostics:
        shown_urns = set()

        for diagnostic in handler.error_diagnostics:
            message = diagnostic.message.strip()

            if _should_skip_diagnostic(message):
                continue

            urn = diagnostic.urn or ""
            if urn and urn in shown_urns:
                continue
            if urn:
                shown_urns.add(urn)

            # Parse and clean the error message
            clean_message = _parse_python_error(message)
            console.print(f"[red]{clean_message}[/red]")

            # For now, just show the first error to avoid spam
            break
    else:
        # Fallback to CommandError
        console.print(f"[red]{e!s}[/red]")

    console.print("\n[bold red]✕ Failed[/bold red]")


def print_operation_header(operation: str, app_name: str, environment: str) -> None:
    console.print(f"{operation} ", style="bold", end="")
    console.print(f"{app_name}", style="bold cyan", end="")
    console.print(" → ", style="dim", end="")
    console.print(f"{environment}", style="bold yellow")


def setup_operation(
    environment: str,
    operation: Literal["deploy", "preview", "refresh", "destroy", "unlock"],
    confirmed_new_app: bool = False,
    show_unchanged: bool = False,
) -> tuple[Stack, str | None, Optional["RichDeploymentHandler"]]:
    with console.status("Loading app..."):
        load_stlv_app()

        # Get app name for display
        app = StelvioApp.get_instance()
        app_name = app._name  # noqa: SLF001

        # Check for app rename for deploy operations
        if operation == "deploy":
            last_deployed_name = get_last_deployed_app_name()
            if last_deployed_name and last_deployed_name != app_name and not confirmed_new_app:
                from stelvio.exceptions import AppRenamedError

                raise AppRenamedError(last_deployed_name, app_name)

        stack = prepare_pulumi_stack(environment)

    # Show header immediately after loading
    operation_titles = {
        "preview": "Diff for",
        "deploy": "Deploying",
        "destroy": "Destroying",
        "refresh": "Refreshing",
        "unlock": "Unlocking",
    }
    print_operation_header(operation_titles[operation], app_name, environment)
    if operation == "unlock":
        return stack, None, None
    # Create event handler with app context
    handler = RichDeploymentHandler(
        app_name, environment, operation, show_unchanged=show_unchanged
    )
    return stack, app_name, handler


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


def run_pulumi_preview(environment: str | None, show_unchanged: bool = False) -> None:
    # Clean active cache tracking files at the start of the run
    clean_function_active_dependencies_caches_file()
    clean_layer_active_dependencies_caches_file()

    stack, app_name, handler = setup_operation(
        environment, "preview", show_unchanged=show_unchanged
    )

    try:
        stack.preview(on_event=handler.handle_event)

        # Cleanup (spinner already started in _handle_summary)
        clean_function_stale_dependency_caches()
        clean_layer_stale_dependency_caches()

        # Show outputs and completion message
        handler.show_completion(stack.outputs())
    except CommandError as e:
        _show_simple_error(e, handler)
        raise SystemExit(1) from None


def run_pulumi_deploy(
    environment: str | None, confirmed_new_app: bool = False, show_unchanged: bool = False
) -> None:
    # Clean active cache tracking files at the start of the run
    clean_function_active_dependencies_caches_file()
    clean_layer_active_dependencies_caches_file()

    stack, app_name, handler = setup_operation(
        environment, "deploy", confirmed_new_app, show_unchanged=show_unchanged
    )

    try:
        stack.up(on_event=handler.handle_event)

        # Cleanup (spinner already started in _handle_summary)
        clean_function_stale_dependency_caches()
        clean_layer_stale_dependency_caches()
        save_deployed_app_name(app_name)

        # Show outputs and completion message
        handler.show_completion(stack.outputs())
    except CommandError as e:
        _show_simple_error(e, handler)
        raise SystemExit(1) from None


def run_pulumi_refresh(environment: str | None) -> None:
    stack, app_name, handler = setup_operation(environment, "refresh")

    try:
        stack.refresh(on_event=handler.handle_event)

        # Show completion message (no cleanup needed for refresh)
        handler.show_completion()
    except CommandError as e:
        _show_simple_error(e, handler)
        raise SystemExit(1) from None


def run_pulumi_destroy(environment: str | None) -> None:
    stack, app_name, handler = setup_operation(environment, "destroy")

    try:
        stack.destroy(on_event=handler.handle_event)
        stack.workspace.remove_stack(environment)

        # Show completion message (no cleanup needed for destroy)
        handler.show_completion()
    except CommandError as e:
        _show_simple_error(e, handler)
        raise SystemExit(1) from None


def run_pulumi_cancel(environment: str | None) -> None:
    stack, _, _ = setup_operation(environment, "unlock")

    stack.cancel()
    console.print("\n[bold green]Unlocked")


def prepare_pulumi_stack(environment: str) -> Stack:
    app = StelvioApp.get_instance()
    project_name = app._name  # noqa: SLF001
    logger.debug("Getting project configuration for environment: %s", environment)
    config = app._execute_user_config_func(environment)  # noqa: SLF001

    _ContextStore.set(AppContext(name=project_name, env=environment, aws=config.aws))

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
    env_vars = {"PULUMI_CONFIG_PASSPHRASE": passphrase}
    if config.aws.region:
        env_vars["AWS_REGION"] = config.aws.region
    if config.aws.profile:
        env_vars["AWS_PROFILE"] = config.aws.profile

    opts = LocalWorkspaceOptions(
        pulumi_command=PulumiCommand(str(get_stelvio_config_dir()), VersionInfo(3, 170, 0)),
        env_vars=env_vars,
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
