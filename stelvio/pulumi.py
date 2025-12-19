import logging
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

import requests
from appdirs import user_config_dir
from pulumi.automation.errors import CommandError
from rich.console import Console

if TYPE_CHECKING:
    from stelvio.rich_deployment_handler import RichDeploymentHandler

logger = logging.getLogger(__name__)
console = Console(soft_wrap=True)


PULUMI_VERSION = "v" + version("pulumi")


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
            # console.print(e.stack_trace, style="dim")

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


def get_stelvio_config_dir() -> Path:
    return Path(user_config_dir(appname="stelvio"))


def get_bin_path() -> Path:
    stelvio_bin_path = get_stelvio_config_dir() / "bin"
    stelvio_bin_path.mkdir(parents=True, exist_ok=True)
    return stelvio_bin_path


def pulumi_path() -> Path:
    executable_name = "pulumi.exe" if sys.platform == "win32" else "pulumi"
    return get_bin_path() / executable_name


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


def ensure_pulumi() -> None:
    """Download Pulumi if not installed or version mismatch."""
    if needs_pulumi():
        with console.status("Downloading Pulumi..."):
            install_pulumi()


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
