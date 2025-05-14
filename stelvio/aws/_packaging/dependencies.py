import hashlib
import logging
import re
import shutil
import subprocess
from collections.abc import Generator, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pulumi import Archive, Asset

from stelvio.project import get_dot_stelvio_dir

type PulumiAssets = Mapping[str, Asset | Archive]

_ACTIVE_CACHE_FILENAME: Final[str] = "active_caches.txt"
_FILE_REFERENCE_PATTERN: Final[re.Pattern] = re.compile(r"^\s*-[rc]\s+(\S+)", re.MULTILINE)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequirementsSpec:
    content: str | None = None
    path_from_root: Path | None = None


def get_or_install_dependencies(  # noqa: PLR0913
    requirements_source: RequirementsSpec,
    runtime: str,
    architecture: str,
    project_root: Path,
    cache_subdirectory: str,
    log_context: str,
) -> Path:
    """
    Checks cache, installs dependencies if needed using uv or pip, and returns
    the absolute path to the populated cache directory.

    Args:
        requirements_source: The source of requirements (content and path_from_root).
        runtime: The target Python runtime (e.g., "python3.12").
        architecture: The target architecture (e.g., "x86_64").
        project_root: Absolute path to the project root.
        cache_subdirectory: Subdirectory within .stelvio/lambda_dependencies
                            (e.g., "functions", "layers").
        log_context: A string identifier for logging (e.g., function or layer name).

    Returns:
        Absolute path to the cache directory containing installed dependencies.

    Raises:
        RuntimeError: If installation fails or required tools (uv/pip) are missing.
        FileNotFoundError: If a referenced requirements file cannot be found.
        ValueError: If requirements paths resolve outside the project root.
    """
    py_version = runtime[6:]  # Assumes format like "python3.12"

    cache_key = _calculate_cache_key(requirements_source, architecture, py_version, project_root)

    dependencies_dir = _get_lambda_dependencies_dir(cache_subdirectory)
    cache_dir = dependencies_dir / cache_key

    _mark_cache_dir_as_active(cache_key, cache_subdirectory)

    if cache_dir.is_dir():
        logger.info("[%s] Cache hit for key '%s'.", log_context, cache_key)
        return cache_dir

    logger.info("[%s] Cache miss for key '%s'. Installing dependencies.", log_context, cache_key)

    cache_dir.mkdir(parents=True, exist_ok=True)
    installer_cmd, install_flags = _get_installer_command(architecture, py_version)
    input_ = None
    if requirements_source.path_from_root is None:
        # Inline requirements: use '-r -' and pass content via stdin
        r_parameter_value = "-"
        input_ = requirements_source.content
        logger.debug("[%s] Using stdin for inline requirements.", log_context)
    else:
        # File-based requirements: use '-r <path>'
        r_parameter_value = str((project_root / requirements_source.path_from_root).resolve())
        logger.debug("[%s] Using requirements file: %s", log_context, r_parameter_value)

    cmd = [
        *installer_cmd,
        "install",
        "-r",
        r_parameter_value,
        "--target",
        str(cache_dir),
        *install_flags,
    ]
    logger.info("[%s] Running dependency installation command: %s", log_context, " ".join(cmd))

    success = _run_install_command(cmd, input_, log_context)

    if success:
        logger.info("[%s] Dependencies installed  into %s.", log_context, cache_dir)
    else:
        shutil.rmtree(cache_dir, ignore_errors=True)
        logger.error(
            "[%s] Installation failed. Cleaning up cache directory: %s", log_context, cache_dir
        )
        raise RuntimeError(
            f"Stelvio: [{log_context}] Failed to install dependencies. Check logs for details."
        )
    return cache_dir


def _resolve_requirements_from_path(
    req_path_str: str, project_root: Path, log_context: str
) -> RequirementsSpec:
    logger.debug("[%s] Requirements specified via path: %s", log_context, req_path_str)
    source_path_relative = Path(req_path_str)
    abs_path = _get_abs_requirements_path(source_path_relative, project_root)
    logger.info("[%s] Reading requirements from specified file: %s", log_context, abs_path)
    return RequirementsSpec(content=None, path_from_root=source_path_relative)


def _resolve_requirements_from_list(
    requirements_list: list[str], log_context: str
) -> RequirementsSpec | None:
    logger.debug("[%s] Requirements specified via inline list.", log_context)
    # Filter out empty/whitespace
    valid_requirements = [r for r in requirements_list if r and r.strip()]
    if not valid_requirements:
        logger.info("[%s] Inline list contains no valid requirement strings.", log_context)
        return None
    content = "\n".join(valid_requirements)
    return RequirementsSpec(content=content, path_from_root=None)


def _normalize_requirements(
    content: str,
    current_file_path_relative: Path | None = None,
    project_root: Path | None = None,
    visited_paths: AbstractSet[Path] | None = None,
) -> Generator[str, None, None]:
    """
    Normalizes requirements content for consistent hashing.
    - Strips leading/trailing whitespace from each line.
    - Removes empty lines.
    - Removes comment lines (starting with '#').
    - If current_file_path_relative is provided, resolves paths in '-r'/'c' lines
      relative to the project root and recursively expands their content.
    - Sorts.
    """
    if visited_paths is None:
        visited_paths = set()

    if current_file_path_relative in visited_paths:
        logger.warning(
            "Circular dependency detected: skipping already visited %s", current_file_path_relative
        )
        return

    visited_paths.add(current_file_path_relative)

    lines = content.splitlines()

    # Determine the directory context for resolving relative paths within this content
    current_dir_abs = None
    if current_file_path_relative and project_root:
        current_dir_abs = (project_root / current_file_path_relative).parent

    for line in lines:
        stripped_line = line.strip()
        if not stripped_line or stripped_line.startswith("#"):
            continue

        line_no_comment = stripped_line.split("#", 1)[0].strip()
        if not line_no_comment:
            continue

        match = _FILE_REFERENCE_PATTERN.match(line_no_comment)
        # Only attempt to resolve if we work with requirements file
        if match and current_dir_abs:
            reference_path_str = match.group(1)
            # Resolve the referenced path relative to the current file's directory
            reference_path_abs = (current_dir_abs / reference_path_str).resolve()
            try:
                reference_path_project_relative = reference_path_abs.relative_to(project_root)
            except ValueError:
                raise ValueError(
                    f"Requirements file '{reference_path_str}' "
                    f"referenced from {current_file_path_relative} "
                    f"resolves to '{reference_path_abs}', "
                    f"which is outside the project root '{project_root}'."
                ) from None
            _validate_is_file_exists(reference_path_abs)
            reference_content = _get_requirements_content(
                reference_path_project_relative, project_root
            )
            yield from _normalize_requirements(
                reference_content, reference_path_project_relative, project_root, visited_paths
            )
        else:
            yield line_no_comment


def _get_requirements_content(relative_path: Path, project_root: Path) -> str:
    abs_path = _get_abs_requirements_path(relative_path, project_root)
    return abs_path.read_text(encoding="utf-8")


def _get_abs_requirements_path(relative_path: Path, project_root: Path) -> Path:
    abs_path = (project_root / relative_path).resolve()

    _validate_is_file_exists(abs_path)

    try:
        abs_path.relative_to(project_root)
    except ValueError:
        raise ValueError(
            f"Requirements file '{relative_path}' resolves to '{abs_path}', "
            f"which is outside the project root '{project_root}'."
        ) from None
    return abs_path


def _validate_is_file_exists(abs_path: Path) -> None:
    if not abs_path.is_file():
        if not abs_path.exists():
            raise FileNotFoundError(f"Requirements file not found: {abs_path}")
        raise ValueError(f"Requirements path is not a file: {abs_path}")


def _calculate_cache_key(
    source: RequirementsSpec, architecture: str, py_version: str, project_root: Path
) -> str:
    """
    Calculates a unique cache key based on requirements content, architecture, and Python version.
    """
    if source.path_from_root is None:
        if bool(_FILE_REFERENCE_PATTERN.search(source.content)):
            raise ValueError(
                "'-r' or '-c' references are not allowed  when providing requirements as list. "
            )
        normalized_requirements = _normalize_requirements(source.content)
    elif isinstance(source.path_from_root, Path):
        content = _get_requirements_content(source.path_from_root, project_root)
        normalized_requirements = _normalize_requirements(
            content, source.path_from_root, project_root
        )
    else:
        raise TypeError(f"Unexpected source identifier type: {type(source.path_from_root)}")
    normalized_requirements_str = "\n".join(sorted(normalized_requirements))
    final_hash = hashlib.sha256(normalized_requirements_str.encode("utf-8")).hexdigest()

    return f"{architecture}__{py_version}__{final_hash[:16]}"


def _run_install_command(cmd: list[str], input_: str, log_context: str) -> bool:
    try:
        result = subprocess.run(cmd, input=input_, capture_output=True, check=True, text=True)  # noqa: S603
        logger.debug("[%s] Installation successful. Stdout:\n%s", log_context, result.stdout)
    except subprocess.CalledProcessError as e:
        # TODO:  test manually to see what error in console
        logger.exception(
            "[%s] Installation command failed.\nCommand: %s\nStderr:\n%s",
            log_context,
            " ".join(e.cmd),
            e.stderr,
        )
        return False
    else:
        return True


def _get_installer_command(architecture: str, py_version: str) -> tuple[list[str], list[str]]:
    """Determines the installer command (uv or pip) and necessary flags."""
    uv_path = shutil.which("uv")
    platform_arch = "aarch64" if architecture == "arm64" else "x86_64"

    if uv_path:
        logger.info("Using 'uv' for dependency installation.")
        installer_cmd = [uv_path, "pip"]
        platform_flags = ["--python-platform", f"{platform_arch}-manylinux2014"]
        implementation_flags = []
    else:
        pip_path = shutil.which("pip")
        if not pip_path:
            raise RuntimeError(
                "Could not find 'pip' or 'uv'. Please ensure one is installed and in your PATH."
            )
        logger.info("Using 'pip' for dependency installation.")
        installer_cmd = [pip_path]
        manylinux_tag = f"manylinux2014_{platform_arch}"
        platform_flags = ["--platform", manylinux_tag]
        implementation_flags = ["--implementation", "cp"]

    install_flags = [
        *implementation_flags,
        *platform_flags,
        "--python-version",
        py_version,
        "--only-binary=:all:",
    ]
    return installer_cmd, install_flags


def _get_lambda_dependencies_dir(cache_subdirectory: str) -> Path:
    return get_dot_stelvio_dir() / "lambda_dependencies" / cache_subdirectory


def clean_active_dependencies_caches_file(cache_subdirectory: str) -> None:
    active_file = _get_lambda_dependencies_dir(cache_subdirectory) / _ACTIVE_CACHE_FILENAME
    active_file.unlink(missing_ok=True)


def _mark_cache_dir_as_active(cache_key: str, cache_subdirectory: str) -> None:
    dependencies_dir = _get_lambda_dependencies_dir(cache_subdirectory)
    dependencies_dir.mkdir(parents=True, exist_ok=True)
    active_file = dependencies_dir / _ACTIVE_CACHE_FILENAME
    with active_file.open("a", encoding="utf-8") as f:
        f.write(f"{cache_key}\n")


def clean_stale_dependency_caches(cache_subdirectory: str) -> None:
    dependencies_dir = _get_lambda_dependencies_dir(cache_subdirectory)
    if not dependencies_dir.is_dir():
        return

    active_file = dependencies_dir / _ACTIVE_CACHE_FILENAME

    if not active_file.is_file():
        return

    active_caches = set(active_file.read_text(encoding="utf-8").splitlines())

    for item in dependencies_dir.iterdir():
        if item.is_dir() and item.name not in active_caches:
            shutil.rmtree(item)
