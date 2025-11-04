import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Final, final

import pulumi
from pulumi import Archive, Asset, AssetArchive, FileArchive, Output
from pulumi_aws.lambda_ import LayerVersion

from stelvio import context
from stelvio.aws._packaging.dependencies import (
    RequirementsSpec,
    _resolve_requirements_from_list,
    _resolve_requirements_from_path,
    clean_active_dependencies_caches_file,
    clean_stale_dependency_caches,
    get_or_install_dependencies,
)
from stelvio.aws.function.constants import DEFAULT_ARCHITECTURE, DEFAULT_RUNTIME
from stelvio.aws.types import AwsArchitecture, AwsLambdaRuntime
from stelvio.component import Component
from stelvio.project import get_project_root

logger = logging.getLogger(__name__)

_LAYER_CACHE_SUBDIR: Final[str] = "layers"


__all__ = ["Layer", "LayerResources"]


@final
@dataclass(frozen=True)
class LayerResources:
    """Represents the AWS resources created for a Stelvio Layer component."""

    layer_version: LayerVersion


@final
class Layer(Component[LayerResources]):
    """
    Represents an AWS Lambda Layer, enabling code and dependency sharing.

    This component manages the creation and versioning of an AWS Lambda LayerVersion
    based on the provided code and/or requirements. Stelvio automatically handles
    packaging according to AWS standards (e.g., placing code under 'python/').

    Args:
        name: The logical name of the layer component within the Stelvio application.
        code: Path (relative to project root) to the directory containing the layer's
              Python code (e.g., "src/my_utils"). The directory itself (e.g., "my_utils")
              will be placed under 'python/' in the layer archive, making it importable
              (e.g., `from my_utils import ...`). If None, the layer contains only dependencies.
        requirements: Specifies Python package dependencies. Accepts a path string to a
                      requirements file (relative to project root), a list of
                      requirement strings (e.g., `["requests", "boto3"]`), or
                      `False` (to explicitly disable). If `None` (default), no
                      dependencies are installed. No default file lookup occurs.
        runtime: The compatible Lambda runtime identifier (e.g., "python3.12").
                 Defaults to the project's default runtime if None.
        architecture: The compatible instruction set architecture (e.g., "x86_64").
                      Defaults to the project's default architecture if None.
    """

    _code: str | None
    _requirements: str | list[str] | bool | None
    _architecture: AwsArchitecture | None
    _runtime: AwsLambdaRuntime | None

    def __init__(
        self,
        name: str,
        *,
        code: str | None = None,
        requirements: str | list[str] | bool | None = None,
        runtime: AwsLambdaRuntime | None = None,
        architecture: AwsArchitecture | None = None,
    ):
        super().__init__(name)
        self._code = code
        self._requirements = requirements
        self._runtime = runtime
        self._architecture = architecture

        if not self._code and not self._requirements:
            raise ValueError(f"Layer '{name}' must specify 'code' and/or 'requirements'.")
        self._validate_requirements()
        # TODO: validate arch and runtime values

    def _validate_requirements(self) -> None:
        if not self._requirements:
            return

        if isinstance(self._requirements, list):
            if not all(isinstance(item, str) for item in self._requirements):
                raise TypeError("If 'requirements' is a list, all its elements must be strings.")
        elif not isinstance(self._requirements, str):
            raise TypeError(
                f"'requirements' must be a string (path), list of strings, or None. "
                f"Got type: {type(self._requirements).__name__}."
            )

    @property
    def runtime(self) -> str | None:
        return self._runtime

    @property
    def architecture(self) -> str | None:
        return self._architecture

    @property
    def arn(self) -> Output[str]:
        return self.resources.layer_version.arn

    def _create_resources(self) -> LayerResources:
        logger.debug("Creating resources for Layer '%s'", self.name)
        log_context = f"Layer: {self.name}"

        runtime = self._runtime or DEFAULT_RUNTIME
        architecture = self._architecture or DEFAULT_ARCHITECTURE

        assets = _gather_layer_assets(
            code=self._code,
            requirements=self._requirements,
            log_context=log_context,
            runtime=runtime,
            architecture=architecture,
        )

        if not assets:
            raise ValueError(
                f"[{log_context}] Layer must contain code or requirements, "
                f"but resulted in an empty package."
            )

        asset_archive = AssetArchive(assets)

        layer_version_resource = LayerVersion(
            context().prefix(self.name),
            layer_name=context().prefix(self.name),
            code=asset_archive,
            compatible_runtimes=[runtime],
            compatible_architectures=[architecture],
        )

        pulumi.export(f"layer_{self.name}_name", layer_version_resource.layer_name)
        pulumi.export(f"layer_{self.name}_version_arn", layer_version_resource.arn)

        return LayerResources(layer_version=layer_version_resource)


def _resolve_requirements_source(
    requirements: str | list[str] | bool | None, project_root: Path, log_context: str
) -> RequirementsSpec | None:
    logger.debug("[%s] Resolving requirements source with option: %r", log_context, requirements)

    if requirements is None or requirements is False or requirements == []:
        logger.debug("[%s] Requirements explicitly disabled or not provided.", log_context)
        return None

    if isinstance(requirements, str):
        return _resolve_requirements_from_path(requirements, project_root, log_context)

    if isinstance(requirements, list):
        return _resolve_requirements_from_list(requirements, log_context)

    raise TypeError(
        f"[{log_context}] Unexpected type for requirements configuration: {type(requirements)}"
    )


def _gather_layer_assets(
    code: str | None,
    requirements: str | list[str] | bool | None,
    log_context: str,
    runtime: str,
    architecture: str,
) -> dict[str, Asset | Archive]:
    assets: dict[str, Asset | Archive] = {}
    project_root = get_project_root()
    if code:
        code_path_relative = Path(code)
        code_path_abs = (project_root / code_path_relative).resolve()

        try:
            _ = code_path_abs.relative_to(project_root)
        except ValueError:
            raise ValueError(
                f"Code path  '{code_path_relative}' resolves to '{code_path_abs}', "
                f"which is outside the project root '{project_root}'."
            ) from None

        if not code_path_abs.is_dir():
            raise ValueError(f"[{log_context}] Code path '{code_path_abs}' is not a directory.")

        code_dir_name = code_path_abs.name
        archive_code_path = f"python/{code_dir_name}"
        logger.debug(
            "[%s] Packaging code directory '%s' into archive path '%s'",
            log_context,
            code_path_abs,
            archive_code_path,
        )
        assets[archive_code_path] = FileArchive(str(code_path_abs))

    source = _resolve_requirements_source(requirements, project_root, log_context)

    if source:
        logger.debug("[%s] Requirements source identified, ensuring installation.", log_context)
        cache_dir = get_or_install_dependencies(
            requirements_source=source,
            runtime=runtime,
            architecture=architecture,
            project_root=project_root,
            cache_subdirectory=_LAYER_CACHE_SUBDIR,
            log_context=log_context,
        )
        # Package the entire cache directory into the standard layer path
        dep_archive_path = f"python/lib/{runtime}/site-packages"
        logger.debug(
            "[%s] Packaging dependency cache '%s' into archive path '%s'",
            log_context,
            cache_dir,
            dep_archive_path,
        )
        # Only add if cache_dir actually exists and has content
        if cache_dir.exists() and any(cache_dir.iterdir()):
            assets[dep_archive_path] = FileArchive(str(cache_dir))
        else:
            logger.warning(
                "[%s] Dependency cache directory '%s' is empty or missing after "
                "installation attempt. No dependencies will be added to the layer.",
                log_context,
                cache_dir,
            )
    return assets


def clean_layer_active_dependencies_caches_file() -> None:
    """Removes the tracking file for active layer dependency caches."""
    clean_active_dependencies_caches_file(_LAYER_CACHE_SUBDIR)


def clean_layer_stale_dependency_caches() -> None:
    """Removes stale cached dependency directories specific to layers."""
    clean_stale_dependency_caches(_LAYER_CACHE_SUBDIR)
