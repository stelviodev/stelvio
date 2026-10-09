from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pulumi import Archive, Asset, AssetArchive, FileArchive, FileAsset, StringAsset, log

from stelvio.project import get_project_root

from .constants import LAMBDA_EXCLUDED_DIRS, LAMBDA_EXCLUDED_EXTENSIONS, LAMBDA_EXCLUDED_FILES
from .dependencies import _get_function_packages

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stelvio.link import Link, Linkable

    from .config import FunctionConfig

_LINKED_FILE_PACKAGE_OVERWRITE_MSG = (
    "Linked files {collisions} conflict with existing paths in the Lambda package. "
    "Choose a different package path for the linked file."
)
_LINKED_FILE_DEPENDENCY_OVERWRITE_MSG = (
    "Linked files {collisions} conflict with dependency paths in the Lambda package. "
    "Choose a different package path for the linked file."
)


def _normalize_package_destination(destination: str) -> str | None:
    """Normalize a link-file destination to a package-relative posix path.

    Returns ``None`` when the path is the package root (``""`` / ``"."``), absolute,
    or still contains ``..`` after ``posixpath.normpath``.
    """
    normalized = posixpath.normpath(destination)
    path = PurePosixPath(normalized)
    if normalized in ("", ".") or path.is_absolute() or ".." in path.parts:
        return None
    return normalized


def _raise_if_linked_files_overwrite_dependencies(
    extra_assets: dict[str, Asset | Archive], dependencies_dir: Path
) -> None:
    collisions = sorted(
        key
        for key in extra_assets
        if (dependencies_dir / key).exists()
        or any((dependencies_dir / parent).is_file() for parent in PurePosixPath(key).parents)
    )
    if collisions:
        raise ValueError(_LINKED_FILE_DEPENDENCY_OVERWRITE_MSG.format(collisions=collisions))


def _raise(error: OSError) -> None:
    raise error


def _folder_assets(folder: Path, folder_name: str) -> dict[str, Asset | Archive]:
    """Every file under the folder as a FileAsset, keyed by its posix path in the package."""
    assets: dict[str, Asset | Archive] = {}
    # An unreadable sub-folder fails the build; walk() would otherwise skip it and the Lambda
    # would fail at import time.
    for root, dirs, files in folder.walk(on_error=_raise):
        dirs[:] = [name for name in dirs if name not in LAMBDA_EXCLUDED_DIRS]
        # walk() does not follow symlinks, so a symlink to a folder is listed among the files.
        for name in files:
            path = root / name
            if name in LAMBDA_EXCLUDED_FILES or path.suffix in LAMBDA_EXCLUDED_EXTENSIONS:
                continue
            relative = path.relative_to(folder).as_posix()
            if path.is_file():
                assets[relative] = FileAsset(path)
            elif path.is_dir():
                # Not followed: code shared between functions belongs in a Layer.
                log.warn(
                    f"Function folder '{folder_name}' has a symlink to a folder, '{relative}', "
                    f"which is not packaged. Shared code belongs in a Layer."
                )
            elif path.is_symlink() and not path.exists():  # a socket or FIFO is not packaged
                raise ValueError(
                    f"Function folder '{folder_name}' has a broken symlink: "
                    f"{relative} -> {path.readlink()}"
                )
    return assets


def _link_file_sources(function_name: str, links: Sequence[Link | Linkable]) -> dict[str, Path]:
    """Resolve and validate LinkConfig._files to normalized package path → local Path.

    Source paths must be absolute. Destinations are normalized once and used as
    AssetArchive keys.
    """
    sources: dict[str, Path] = {}
    for item in links:
        link = item.link()
        for zip_path, local_path in (link._files or {}).items():  # noqa: SLF001
            destination = _normalize_package_destination(zip_path)
            if destination is None:
                raise ValueError(
                    f"Link '{link.name}' puts a file at '{zip_path}' in Function "
                    f"'{function_name}', but package paths must be relative and stay inside "
                    f"the package."
                )
            source = Path(local_path)
            if not source.is_absolute():
                raise ValueError(
                    f"Link '{link.name}' puts {local_path!r} into Function '{function_name}', "
                    f"but linked file sources must be absolute paths."
                )
            if not source.is_file():
                reason = (
                    "but that file does not exist."
                    if not source.exists()
                    else "but that path is not a file."
                )
                raise ValueError(
                    f"Link '{link.name}' puts {source} into Function '{function_name}', {reason}"
                )
            for existing, existing_source in sources.items():
                if existing == destination:
                    if existing_source != source:
                        raise ValueError(
                            f"Link '{link.name}' puts {source} at '{destination}' in Function "
                            f"'{function_name}', but '{destination}' is already mapped to "
                            f"{existing_source}."
                        )
                    break
                if existing.startswith(destination + "/") or destination.startswith(
                    existing + "/"
                ):
                    raise ValueError(
                        f"Link '{link.name}' puts a file at '{destination}' in Function "
                        f"'{function_name}', but that path conflicts with '{existing}' "
                        f"(a file and a directory cannot share the same path prefix)."
                    )
            else:
                sources[destination] = source
    return sources


def _create_lambda_archive(
    function_config: FunctionConfig,
    resource_file_content: str | None,
    extra_assets: dict[str, Asset | Archive] | None = None,
) -> AssetArchive:
    """Create an AssetArchive for Lambda function based on configuration.
    Handles both single file and folder-based Lambdas.
    """

    project_root = get_project_root()

    assets: dict[str, Asset | Archive] = {}
    handler_file = str(Path(function_config.handler_file_path).with_suffix(".py"))
    if function_config.folder_path:
        # Handle folder-based Lambda
        full_folder_path = project_root / function_config.folder_path
        if not full_folder_path.exists():
            raise ValueError(f"Folder not found: {full_folder_path}")

        # Check if handler file exists in the folder
        absolute_handler_file = full_folder_path / handler_file
        if not absolute_handler_file.exists():
            raise ValueError(f"Handler file not found in folder: {absolute_handler_file}.py")

        assets |= _folder_assets(full_folder_path, function_config.folder_path)
    # Handle single file Lambda
    else:
        absolute_handler_file = project_root / handler_file
        if not absolute_handler_file.exists():
            raise ValueError(f"Handler file not found: {absolute_handler_file}")
        assets[absolute_handler_file.name] = FileAsset(absolute_handler_file)

    if resource_file_content:
        assets["stlv_resources.py"] = StringAsset(resource_file_content)

    if extra_assets:
        collisions = sorted(
            key
            for key in extra_assets
            if any(
                key == existing or key.startswith(existing + "/") or existing.startswith(key + "/")
                for existing in assets
            )
        )
        if collisions:
            raise ValueError(_LINKED_FILE_PACKAGE_OVERWRITE_MSG.format(collisions=collisions))
        assets |= extra_assets

    dependencies_dir = _get_function_packages(function_config)
    if dependencies_dir:
        if extra_assets:
            _raise_if_linked_files_overwrite_dependencies(extra_assets, dependencies_dir)
        assets[""] = FileArchive(str(dependencies_dir))  # dependencies sit at the package root
    return AssetArchive(assets)
