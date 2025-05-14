import logging
from pathlib import Path
from typing import Final

from pulumi import FileArchive

from stelvio.aws._packaging.dependencies import (
    PulumiAssets,
    RequirementsSpec,
    _resolve_requirements_from_list,
    _resolve_requirements_from_path,
    clean_active_dependencies_caches_file,
    clean_stale_dependency_caches,
    get_or_install_dependencies,
)
from stelvio.aws.function.config import FunctionConfig
from stelvio.aws.function.constants import DEFAULT_ARCHITECTURE, DEFAULT_RUNTIME
from stelvio.project import get_project_root

# Constants specific to function dependency resolution
_REQUIREMENTS_FILENAME: Final[str] = "requirements.txt"
_FUNCTION_CACHE_SUBDIR: Final[str] = "functions"

logger = logging.getLogger(__name__)


def _get_function_packages(function_config: FunctionConfig) -> PulumiAssets | None:
    """
    Resolves, installs (via shared logic), and packages Lambda function dependencies.

    Args:
        function_config: The configuration object for the Lambda function.

    Returns:
        A dictionary mapping filenames/paths to Pulumi Assets/Archives for the
        dependencies, or None if no requirements are specified or found.
    """
    project_root = get_project_root()
    log_context = f"Function: {function_config.handler}"  # Use handler for context
    logger.debug("[%s] Starting dependency resolution", log_context)

    # 1. Resolve requirements source
    source = _resolve_requirements_source(function_config, project_root, log_context)
    if source is None:
        logger.debug("[%s] No requirements source found or requirements disabled.", log_context)
        return None

    # 2. Determine runtime, architecture, and source context path for shared functions
    runtime = function_config.runtime or DEFAULT_RUNTIME
    architecture = function_config.architecture or DEFAULT_ARCHITECTURE

    # 3. Ensure Dependencies are Installed (using shared function)
    cache_dir = get_or_install_dependencies(
        requirements_source=source,
        runtime=runtime,
        architecture=architecture,
        project_root=project_root,
        cache_subdirectory=_FUNCTION_CACHE_SUBDIR,
        log_context=log_context,
    )

    # 4. Package dependencies from the cache directory (using shared function)
    return {"": FileArchive(str(cache_dir))}


def _handle_requirements_none(
    config: FunctionConfig, project_root: Path, log_context: str
) -> RequirementsSpec | None:
    """Handle the case where requirements=None (default lookup)."""
    logger.debug(
        "[%s] Requirements option is None, looking for default %s",
        log_context,
        _REQUIREMENTS_FILENAME,
    )
    if config.folder_path:  # Folder-based: look inside the folder
        base_folder_relative = Path(config.folder_path)
    else:  # Single file lambda: relative to the handler file's directory
        base_folder_relative = Path(config.handler_file_path).parent

    # Path relative to project root
    source_path_relative = base_folder_relative / _REQUIREMENTS_FILENAME
    abs_path = project_root / source_path_relative
    logger.debug("[%s] Checking for default requirements file at: %s", log_context, abs_path)

    if abs_path.is_file():
        logger.info("[%s] Found default requirements file: %s", log_context, abs_path)
        return RequirementsSpec(content=None, path_from_root=source_path_relative)
    logger.debug("[%s] Default %s not found.", log_context, _REQUIREMENTS_FILENAME)
    return None


def _resolve_requirements_source(
    config: FunctionConfig, project_root: Path, log_context: str
) -> RequirementsSpec | None:
    """
    Determines the source and content of requirements based on FunctionConfig.

    Returns:
        A RequirementsSource, or None if no requirements are applicable.
    Raises:
        FileNotFoundError: If an explicitly specified requirements file is not found.
        ValueError: If an explicitly specified path is not a file.
    """
    requirements = config.requirements
    logger.debug("[%s] Resolving requirements source with option: %r", log_context, requirements)

    if requirements is False or requirements == []:
        logger.info(
            "[%s] Requirements handling explicitly disabled or empty list provided.", log_context
        )
        return None

    if requirements is None:
        return _handle_requirements_none(config, project_root, log_context)

    if isinstance(requirements, str):
        return _resolve_requirements_from_path(requirements, project_root, log_context)

    if isinstance(requirements, list):
        return _resolve_requirements_from_list(requirements, log_context)

    # Should be caught by FunctionConfig validation, but raise defensively
    raise TypeError(
        f"[{log_context}] Unexpected type for requirements configuration: {type(requirements)}"
    )


def clean_function_active_dependencies_caches_file() -> None:
    clean_active_dependencies_caches_file(_FUNCTION_CACHE_SUBDIR)


def clean_function_stale_dependency_caches() -> None:
    clean_stale_dependency_caches(_FUNCTION_CACHE_SUBDIR)
