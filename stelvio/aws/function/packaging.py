from pathlib import Path

from pulumi import Archive, Asset, AssetArchive, FileAsset, StringAsset

from stelvio.project import get_project_root

from .config import FunctionConfig
from .constants import LAMBDA_EXCLUDED_DIRS, LAMBDA_EXCLUDED_EXTENSIONS, LAMBDA_EXCLUDED_FILES
from .dependencies import _get_function_packages


def _create_lambda_archive(
    function_config: FunctionConfig,
    resource_file_content: str | None,
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

    function_packages_archives = _get_function_packages(function_config)
    if function_packages_archives:
        assets |= function_packages_archives
    return AssetArchive(assets)
