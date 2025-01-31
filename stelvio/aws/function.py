import os
from dataclasses import field
from functools import cache
from pathlib import Path
from typing import Dict, Sequence, Any, TypedDict, Unpack, ClassVar
from typing import final

import pulumi
from pulumi import (
    AssetArchive,
    FileAsset,
    Output,
    StringAsset,
    Asset,
)
from pulumi_aws import lambda_, iam

from stelvio.component import ComponentRegistry, Component
from stelvio.link import Link, Linkable

DEFAULT_RUNTIME = "python3.12"

LAMBDA_EXCLUDED_FILES = ["stlv.py", ".DS_Store"]  # exact file matches
LAMBDA_EXCLUDED_DIRS = ["__pycache__"]  # exact directory matches
LAMBDA_EXCLUDED_EXTENSIONS = [".pyc"]  # file extensions


# "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole",
LAMBDA_BASIC_EXECUTION_ROLE = (
    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
)

from dataclasses import dataclass


@dataclass(frozen=True, kw_only=True)
class FunctionConfig:
    # handler is mandatory but rest defaults to None. Default values will be configured
    # elsewhere not here so they can be configurable and so they don't cause trouble
    # in api Gateway where we check for conflicting configurations
    handler: str
    folder: str | None = None
    links: list[Link | Linkable] = field(default_factory=list)
    memory_size: int | None = None
    timeout: int | None = None
    environment: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        # Split handler by :: to check if using folder:handler format
        folder_parts = self.handler.split("::")

        if len(folder_parts) > 2:
            raise ValueError("Handler can only contain one :: separator")

        # If handler contains ::, validate folder and handler parts
        if len(folder_parts) == 2:
            if self.folder is not None:
                raise ValueError(
                    "Cannot specify folder both in handler (::) and as folder parameter"
                )
            folder_path, handler_path = folder_parts
            # Validate the extracted folder path
            if "." in folder_path:
                raise ValueError("Folder path should not contain dots")
        else:
            # If no :: in handler, validate any directly provided folder
            if self.folder is not None and "." in self.folder:
                raise ValueError("Folder path should not contain dots")
            handler_path = self.handler

        # Validate handler format: path/to/file.function_name
        if "." not in handler_path:
            raise ValueError(
                "Handler must contain a dot separator between file path and function "
                "name"
            )

        # Validate there's actual content before and after the dot
        file_path, function_name = handler_path.rsplit(".", 1)
        if not file_path or not function_name:
            raise ValueError("Both file path and function name must be non-empty")

        # Ensure file path doesn't contain any dots
        if "." in file_path:
            raise ValueError("File path part should not contain dots")

    @property
    def has_folder(self) -> bool:
        return self.folder is not None or "::" in self.handler

    @property
    def folder_path(self) -> str | None:
        if self.folder is not None:
            return self.folder
        return self.handler.split("::")[0] if self.has_folder else None

    @property
    def _handler_part(self) -> str:
        return self.handler.split("::")[1] if "::" in self.handler else self.handler

    @property
    def handler_file_path(self) -> str:
        # replace remaining dots with /  for non-root handler files?
        return self._handler_part.rsplit(".", 1)[0]

    @property
    def local_handler_file_path(self) -> str:
        # replace remaining dots with /  for non-root handler files?
        return self.handler_format.rsplit(".", 1)[0]

    @property
    def handler_function_name(self) -> str:
        return self._handler_part.rsplit(".", 1)[1]

    @property
    def handler_format(self) -> str:
        # If using folder, return just the handler part
        # For single file lambda, return everything after last slash
        return self._handler_part if self.has_folder else self.handler.split("/")[-1]


class FunctionConfigDict(TypedDict, total=False):
    handler: str
    folder: str
    links: list[Link | Linkable]
    memory_size: int
    timeout: int
    environment: dict[str, str]


# TODO: Think about what to make public interface/properties
@final
class Function(Component[lambda_.Function]):
    """AWS Lambda function component with automatic resource discovery.

    Generated environment variables follow pattern: STLV_RESOURCENAME_PROPERTYNAME

    Args:
        name: Function name
        config: Complete function configuration as FunctionConfig or dict
        **opts: Individual function configuration parameters

    You can configure the function in two ways:
        - Provide complete config:
            function = Function(
                name="process-user",
                config={"handler": "functions/orders.list", "timeout": 30}
            )
        - Provide individual parameters:
            function = Function(
                name="process-user",
                handler="functions/orders.list",
                links=[table.default_link(), bucket.readonly_link()]
            )
    """

    _config: FunctionConfig

    def __init__(
        self,
        name: str,
        config: None | FunctionConfig | FunctionConfigDict = None,
        **opts: Unpack[FunctionConfigDict],
    ):
        super().__init__(name)
        self._config = self._parse_config(config, opts)

    @staticmethod
    def _parse_config(
        config: None | FunctionConfig | FunctionConfigDict,
        opts: dict,
    ) -> FunctionConfig:
        if not config and not opts:
            raise ValueError(
                "Missing function handler: must provide either a complete "
                "configuration via 'config' parameter or at least the 'handler' option"
            )
        if config and opts:
            raise ValueError(
                "Invalid configuration: cannot combine 'config' parameter with "
                "additional options - provide all settings either in 'config' or as "
                "separate options"
            )

        if config is None:
            return FunctionConfig(**opts)
        if isinstance(config, FunctionConfig):
            return config
        if isinstance(config, dict):
            return FunctionConfig(**config)

        raise TypeError(
            "Invalid config type: expected FunctionConfig or dict, got "
            f"{type(config).__name__}"
        )

    @property
    def config(self) -> FunctionConfig:
        return self._config

    @property
    def invoke_arn(self) -> Output[str]:
        return self._resource.invoke_arn

    @property
    def resource_name(self) -> Output[str]:
        return self._resource.name

    def _create_resource(self) -> lambda_.Function:
        iam_statements = _extract_links_permissions(self._config.links)
        links_props = _extract_links_property_mappings(self._config.links)

        function_policy = _create_function_policy(self.name, iam_statements)
        lambda_role = _create_lambda_role(self.name)
        _attach_role_policies(self.name, lambda_role, function_policy)

        # https://docs.aws.amazon.com/lambda/latest/dg/configuration-envvars.html

        if self.config.has_folder:
            folder_path = self.config.folder_path
        else:
            folder_path = str(Path(self.config.handler_file_path).parent)

        lambda_resource_file_content = create_stlv_resource_file_content(links_props)

        LinkPropertiesRegistry.add(folder_path, links_props)

        ide_resource_file_content = create_stlv_resource_file_content(
            LinkPropertiesRegistry.get_link_properties_map(folder_path)
        )

        _create_stlv_resource_file(
            get_project_root() / folder_path, ide_resource_file_content
        )
        extra_assets_map = FunctionAssetsRegistry.get_assets_map(self)
        handler = self.config.handler_format
        if "stlv_routing_handler.py" in extra_assets_map:
            handler = "stlv_routing_handler.lambda_handler"
        function_resource = lambda_.Function(
            self.name,
            role=lambda_role.arn,
            runtime=DEFAULT_RUNTIME,
            code=_create_lambda_archive(
                self.config,
                lambda_resource_file_content,
                extra_assets_map,
            ),
            handler=handler,
            environment={"variables": _extract_links_env_vars(self._config.links)},
            memory_size=self.config.memory_size,
            timeout=self.config.timeout,
        )

        ComponentRegistry.add_instance_output(self, function_resource)
        pulumi.export(f"lambda_{self.name}_arn", function_resource.arn)
        return function_resource


class LinkPropertiesRegistry:
    _folder_links_properties_map: ClassVar[dict[str, dict[str, list[str]]]] = {}

    @classmethod
    def add(cls, folder: str, link_properties_map: dict[str, list[str]]):
        cls._folder_links_properties_map.setdefault(folder, {}).update(
            link_properties_map
        )

    @classmethod
    def get_link_properties_map(cls, folder: str) -> dict[str, list[str]]:
        return cls._folder_links_properties_map.get(folder, {})


class FunctionAssetsRegistry:
    _functions_assets_map: ClassVar[dict[Function, dict[str, Asset]]] = {}

    @classmethod
    def add(cls, function_: Function, assets_map: dict[str, Asset]):
        cls._functions_assets_map.setdefault(function_, {}).update(assets_map)

    @classmethod
    def get_assets_map(cls, function_: Function) -> dict[str, Asset]:
        return cls._functions_assets_map.get(function_, {}).copy()


def _extract_links_permissions(linkables: Sequence[Link | Linkable]) -> list[dict]:
    """Extracts IAM statements from permissions for function's IAM policy"""
    return [
        permission.to_provider_format()
        for linkable in linkables
        for permission in linkable.link().permissions
    ]


def _envar_name(link_name: str, prop_name: str) -> str:
    cleaned_link_name = "".join(c if c.isalnum() else "_" for c in link_name)

    if (first_char := cleaned_link_name[0]) and first_char.isdigit():
        cleaned_link_name = NUMBER_WORDS[int(first_char)] + cleaned_link_name[1:]

    return f"STLV_{cleaned_link_name.upper()}_{prop_name.upper()}"


def _extract_links_env_vars(linkables: Sequence[Link | Linkable]) -> dict[str, str]:
    """
    Creates environment variables with STLV_ prefix for runtime resource discovery.
    The STLV_ prefix in environment variables ensures no conflicts with other env vars
    and makes it clear which variables are managed by Stelvio.
    """
    link_objects = [item.link() for item in linkables]
    return {
        _envar_name(link.name, prop_name): value
        for link in link_objects
        for prop_name, value in link.properties.items()
    }


def _extract_links_property_mappings(
    linkables: Sequence[Link | Linkable],
) -> dict[str, list[str]]:
    """Maps resource properties to Python class names for code generation of resource access classes."""
    link_objects = [item.link() for item in linkables]
    return {link.name: [p for p in link.properties] for link in link_objects}


def _create_function_policy(
    name: str, statements: list[dict[str, Any]]
) -> iam.Policy | None:
    """Create IAM policy for Lambda if there are any statements."""
    if not statements:
        return None

    policy_document = iam.get_policy_document(statements=statements)
    return iam.Policy(
        f"{name}-Policy",
        name=f"{name}-Policy",
        path="/",
        policy=policy_document.json,
    )


def _create_lambda_role(name: str) -> iam.Role:
    """Create basic execution role for Lambda."""
    assume_role_policy = iam.get_policy_document(
        statements=[
            iam.GetPolicyDocumentStatementArgs(
                actions=["sts:AssumeRole"],
                principals=[
                    iam.GetPolicyDocumentStatementPrincipalArgs(
                        identifiers=["lambda.amazonaws.com"], type="Service"
                    )
                ],
            )
        ]
    )
    return iam.Role(f"{name}-Role", assume_role_policy=assume_role_policy.json)


def _attach_role_policies(
    name: str, role: iam.Role, function_policy: iam.Policy | None
) -> None:
    """Attach required policies to Lambda role."""
    iam.RolePolicyAttachment(
        f"{name}-BasicExecutionRolePolicyAttachment",
        role=role.name,
        policy_arn=LAMBDA_BASIC_EXECUTION_ROLE,
    )
    if function_policy:
        iam.RolePolicyAttachment(
            f"{name}-DefaultRolePolicyAttachment",
            role=role.name,
            policy_arn=function_policy.arn,
        )


def _create_lambda_archive(
    function_config: FunctionConfig,
    resource_file_content: str | None,
    extra_assets_map: dict[str, Asset],
) -> AssetArchive:
    """
    Create an AssetArchive for Lambda function based on configuration.
    Handles both single file and folder-based Lambdas.
    """
    project_root = get_project_root()
    assets = extra_assets_map
    handler_file = str(Path(function_config.handler_file_path).with_suffix(".py"))
    if function_config.has_folder:
        # Handle folder-based Lambda
        folder_path = project_root / function_config.folder_path
        if not folder_path.exists():
            raise ValueError(f"Folder not found: {folder_path}")

        # Check if handler file exists in the folder
        if handler_file not in extra_assets_map:
            absolute_handler_file = folder_path / handler_file
            if not absolute_handler_file.exists():
                raise ValueError(
                    f"Handler file not found in folder: {absolute_handler_file}.py"
                )

        # Recursively collect all files from the folder
        assets |= {
            str(file_path.relative_to(folder_path)): FileAsset(file_path)
            for file_path in folder_path.rglob("*")
            if not (
                file_path.is_dir()
                or file_path.name in LAMBDA_EXCLUDED_FILES
                or file_path.parent.name in LAMBDA_EXCLUDED_DIRS
                or file_path.suffix in LAMBDA_EXCLUDED_EXTENSIONS
            )
        }
    else:
        # Handle single file Lambda
        if handler_file not in extra_assets_map:
            absolute_handler_file = project_root / handler_file
            if not absolute_handler_file.exists():
                raise ValueError(f"Handler file not found: {absolute_handler_file}")
            assets[absolute_handler_file.name] = FileAsset(absolute_handler_file)
    if resource_file_content:
        assets["stlv_resources.py"] = StringAsset(resource_file_content)
    return AssetArchive(assets)


def _create_stlv_resource_file(folder: Path, content: str) -> None:
    """Create resource access file with supplied content."""
    path = folder / "stlv_resources.py"

    # Delete file if no content
    if not content:
        path.unlink(missing_ok=True)
        return

    with open(path, "w") as f:
        f.write(content)


def _single_file_create_stlv_resource_file(
    folder: Path, link_properties_map: Dict[str, list[str]]
) -> None:
    """Generate resource access file with classes for linked resources."""
    path = folder / "stlv_resources.py"

    # Delete file if no properties to generate
    if not any(link_properties_map.values()):
        path.unlink(missing_ok=True)
        return
    with open(path, "w") as f:
        f.write("import os\n")
        f.write("from dataclasses import dataclass\n")
        f.write("from typing import Final\n")
        f.write("from functools import cached_property\n\n\n")

        for link_name, properties in link_properties_map.items():
            if not properties:
                continue
            class_name = _to_valid_python_class_name(link_name)
            f.write("@dataclass(frozen=True)\n")
            f.write(f"class {class_name}Resource:\n")
            for prop in properties:
                f.write(f"    @cached_property\n    def {prop}(self) -> str:\n")
                f.write(
                    f'        return os.getenv("{_envar_name(link_name, prop)}")\n\n'
                )
            f.write("\n")

        f.write("@dataclass(frozen=True)\n")
        f.write("class LinkedResources:\n")
        for link_name in link_properties_map:
            class_name = _to_valid_python_class_name(link_name)
            f.write(
                f"    {_pascal_to_camel(class_name)}: Final[{class_name}Resource] = {class_name}Resource()\n"
            )
        f.write("\n\n")

        f.write("Resources: Final = LinkedResources()\n")


def create_stlv_resource_file_content(
    link_properties_map: Dict[str, list[str]]
) -> str | None:
    """Generate resource access file content with classes for linked resources."""
    # Return None if no properties to generate
    if not any(link_properties_map.values()):
        return None

    lines = [
        "import os",
        "from dataclasses import dataclass",
        "from typing import Final",
        "from functools import cached_property\n\n",
    ]

    for link_name, properties in link_properties_map.items():
        if not properties:
            continue
        lines.extend(_create_link_resource_class(link_name, properties))

    lines.extend(["@dataclass(frozen=True)", "class LinkedResources:"])

    # and this
    for link_name in link_properties_map:
        class_name = _to_valid_python_class_name(link_name)
        lines.append(
            f"    {_pascal_to_camel(class_name)}: Final[{class_name}Resource] = {class_name}Resource()"
        )
    lines.extend(["\n", "Resources: Final = LinkedResources()"])

    return "\n".join(lines)


def _create_link_resource_class(link_name, properties) -> list[str] | None:
    if not properties:
        return None
    lines = []
    class_name = _to_valid_python_class_name(link_name)
    lines.append("@dataclass(frozen=True)")
    lines.append(f"class {class_name}Resource:")
    for prop in properties:
        lines.extend(
            [
                f"    @cached_property",
                f"    def {prop}(self) -> str:",
                f'        return os.getenv("{_envar_name(link_name, prop)}")\n',
            ]
        )
    lines.append("")
    return lines


NUMBER_WORDS = {
    "0": "Zero",
    "1": "One",
    "2": "Two",
    "3": "Three",
    "4": "Four",
    "5": "Five",
    "6": "Six",
    "7": "Seven",
    "8": "Eight",
    "9": "Nine",
}


def _to_valid_python_class_name(aws_name: str) -> str:
    # Split and clean
    words = aws_name.replace("-", " ").replace(".", " ").replace("_", " ").split()
    cleaned_words = ["".join(c for c in word if c.isalnum()) for word in words]
    class_name = "".join(word.capitalize() for word in cleaned_words)

    # Convert only first digit if name starts with number
    if class_name and class_name[0].isdigit():
        first_digit = NUMBER_WORDS[class_name[0]]
        class_name = first_digit + class_name[1:]

    return class_name


def _pascal_to_camel(pascal_str: str) -> str:
    """Convert Pascal case to camel case.
    Example: PascalCase -> pascalCase, XMLParser -> xmlParser
    """
    if not pascal_str:
        return pascal_str
    i = 1
    while i < len(pascal_str) and pascal_str[i].isupper():
        i += 1
    return pascal_str[:i].lower() + pascal_str[i:]


# It's here for now but probably will be moved to some other place as it's used in
# other places too
@cache
def get_project_root() -> Path:
    """
    Find and cache the project root by looking for stlv_app.py.
    Raises ValueError if not found.
    """
    start_path = Path(os.getcwd()).resolve()

    current = start_path
    while current != current.parent:
        if (current / "stlv_app.py").exists():
            return current
        current = current.parent

    raise ValueError(
        "Could not find project root: no stlv_app.py found in parent directories"
    )
