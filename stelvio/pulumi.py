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
    ConfigValue,
    LocalWorkspaceOptions,
    ProjectBackend,
    ProjectSettings,
    PulumiCommand,
    create_or_select_stack,
    fully_qualified_stack_name,
)
from semver import VersionInfo

from stelvio.project import get_project_root

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


def pulumi_program() -> None:
    logger.debug("CWD")
    logger.debug(Path.cwd())
    logger.debug("SYS PATH")
    logger.debug(sys.path)

    original_sys_path = list(sys.path)  # Make a copy
    project_root = get_project_root()
    logger.debug("PROJECT ROOT")
    logger.debug(project_root)
    if project_root not in sys.path:
        sys.path.insert(0, str(project_root))
    try:
        import_module("stlv_app")
    finally:
        sys.path = original_sys_path


def run_pulumi() -> None:
    project_name = "sample_stelvio_proj"
    stack_name = fully_qualified_stack_name("organization", project_name, "dev")

    state_dir_path = get_stelvio_config_dir()
    backend = ProjectBackend(f"file://{state_dir_path}")
    project_settings = ProjectSettings(
        name=project_name,
        runtime="python",
        backend=backend,
    )

    opts = LocalWorkspaceOptions(
        pulumi_command=PulumiCommand(str(get_stelvio_config_dir()), VersionInfo(3, 170, 0)),
        env_vars={"PULUMI_CONFIG_PASSPHRASE": "test"},
        project_settings=project_settings,
        pulumi_home=str(get_stelvio_config_dir() / ".pulumi"),
    )

    stack = create_or_select_stack(
        stack_name=stack_name,
        project_name=project_name,
        program=pulumi_program,
        opts=opts,
    )
    logger.debug("Successfully initialized stack")

    # logger.debug("Installing plugins...")
    # stack.workspace.install_plugin("aws", "v6.80.0")
    # logger.debug("Plugins installed")

    # set stack configuration specifying the AWS region to deploy
    logger.debug("Setting up config")
    stack.set_config("aws:region", ConfigValue(value="us-east-1"))
    logger.debug("Config set")

    # logger.debug("Refreshing stack...")
    # stack.refresh(on_output=print, color="always")
    # logger.debug("Refresh complete")

    logger.debug("Updating stack...")
    up_res = stack.preview(on_output=print, color="always")
    # To destroy:
    # print(f"Destroying stack '{STACK_NAME}'...")
    # stack.destroy(on_output=print)
    # print("Stack destroyed.")
    # workspace.remove_stack(STACK_NAME)
    # print("Stack removed from backend.")


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
