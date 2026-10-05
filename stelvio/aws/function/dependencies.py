import logging
from pathlib import Path
from typing import Final

from stelvio.aws._packaging.dependencies import (
    RequirementsSpec,
    get_or_install_dependencies,
    requirements_spec,
)
from stelvio.aws.function.config import FunctionConfig
from stelvio.aws.types import DEFAULT_ARCHITECTURE, DEFAULT_RUNTIME
from stelvio.project import get_project_root

# Constants specific to function dependency resolution
_REQUIREMENTS_FILENAME: Final[str] = "requirements.txt"
_FUNCTION_CACHE_SUBDIR: Final[str] = "functions"

logger = logging.getLogger(__name__)


def _get_function_packages(function_config: FunctionConfig) -> Path | None:
    """Installs the function's dependencies into the shared cache and returns the directory
    holding them, or None when the function has none."""
    project_root = get_project_root()
    log_context = f"Function: {function_config.handler}"  # Use handler for context
    logger.debug("[%s] Starting dependency resolution", log_context)

    if function_config.requirements is None:
        source = _handle_requirements_none(function_config, project_root, log_context)
    else:
        source = requirements_spec(function_config.requirements)
    if source is None:
        logger.debug("[%s] No requirements source found or requirements disabled.", log_context)
        return None

    return get_or_install_dependencies(
        requirements_source=source,
        runtime=function_config.runtime or DEFAULT_RUNTIME,
        architecture=function_config.architecture or DEFAULT_ARCHITECTURE,
        project_root=project_root,
        cache_subdirectory=_FUNCTION_CACHE_SUBDIR,
        log_context=log_context,
    )


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
