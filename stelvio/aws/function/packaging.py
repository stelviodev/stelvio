import posixpath
from pathlib import Path, PurePosixPath

from pulumi import Archive, Asset, AssetArchive, FileArchive, FileAsset, StringAsset

from stelvio.project import get_project_root

from .config import FunctionConfig
from .constants import LAMBDA_EXCLUDED_DIRS, LAMBDA_EXCLUDED_EXTENSIONS, LAMBDA_EXCLUDED_FILES
from .dependencies import _get_function_packages

_LINKED_FILE_OVERWRITE_MSG = (
    "Linked files {collisions} would overwrite files in the Lambda package. "
    "Rename or move those files in your function's folder."
)


def _normalize_package_destination(destination: str) -> str:
    """Normalize a Link.files destination to a package-relative posix path.

    Rejects the package root (``""`` / ``"."``), absolute paths, and any path that
    still contains ``..`` after ``posixpath.normpath``.
    """
    normalized = posixpath.normpath(destination)
    path = PurePosixPath(normalized)
    if normalized in ("", ".") or path.is_absolute() or ".." in path.parts:
        raise ValueError(
            f"Package path must be relative and stay inside the package, got {destination!r}."
        )
    return normalized


def _raise_linked_file_overwrite(collisions: list[str]) -> None:
    raise ValueError(_LINKED_FILE_OVERWRITE_MSG.format(collisions=collisions))


def _dependency_file_paths(cache_dir: Path) -> set[str]:
    """Relative posix paths of files under a pip dependency cache directory."""
    return {
        file_path.relative_to(cache_dir).as_posix()
        for file_path in cache_dir.rglob("*")
        if file_path.is_file()
    }


def _raise_if_linked_files_overwrite_dependencies(
    extra_assets: dict[str, Asset | Archive],
    function_packages_archives: dict[str, Asset | Archive],
) -> None:
    root_archive = function_packages_archives.get("")
    if not isinstance(root_archive, FileArchive):
        return
    cache_dir = Path(root_archive.path)
    if not cache_dir.is_dir():
        return
    if collisions := sorted(_dependency_file_paths(cache_dir) & extra_assets.keys()):
        _raise_linked_file_overwrite(collisions)


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

        # Recursively collect all files from the folder
        assets |= {
            str(file_path.relative_to(full_folder_path)): FileAsset(file_path)
            for file_path in full_folder_path.rglob("*")
            if not (
                file_path.is_dir()
                or file_path.name in LAMBDA_EXCLUDED_FILES
                or file_path.parent.name in LAMBDA_EXCLUDED_DIRS
                or file_path.suffix in LAMBDA_EXCLUDED_EXTENSIONS
            )
        }
    # Handle single file Lambda
    else:
        absolute_handler_file = project_root / handler_file
        if not absolute_handler_file.exists():
            raise ValueError(f"Handler file not found: {absolute_handler_file}")
        assets[absolute_handler_file.name] = FileAsset(absolute_handler_file)

    if resource_file_content:
        assets["stlv_resources.py"] = StringAsset(resource_file_content)

    if extra_assets:
        if collisions := sorted(assets.keys() & extra_assets.keys()):
            _raise_linked_file_overwrite(collisions)
        assets |= extra_assets

    function_packages_archives = _get_function_packages(function_config)
    if function_packages_archives:
        if extra_assets:
            _raise_if_linked_files_overwrite_dependencies(extra_assets, function_packages_archives)
        assets |= function_packages_archives
    return AssetArchive(assets)
