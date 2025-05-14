from collections import Counter
from dataclasses import MISSING, Field, dataclass, field, fields
from typing import Literal, TypedDict

from stelvio.aws.function.constants import DEFAULT_ARCHITECTURE, DEFAULT_RUNTIME, MAX_LAMBDA_LAYERS
from stelvio.aws.layer import Layer
from stelvio.aws.types import AwsArchitecture, AwsLambdaRuntime
from stelvio.link import Link, Linkable


class FunctionConfigDict(TypedDict, total=False):
    handler: str
    folder: str
    links: list[Link | Linkable]
    memory: int
    timeout: int
    environment: dict[str, str]
    architecture: AwsArchitecture
    runtime: AwsLambdaRuntime
    requirements: str | list[str] | Literal[False] | None
    layers: list[Layer] | None


@dataclass(frozen=True, kw_only=True)
class FunctionConfig:
    # handler is mandatory but rest defaults to None. Default values will be configured
    # elsewhere not here so they can be configurable and so they don't cause trouble
    # in api Gateway where we check for conflicting configurations
    handler: str
    folder: str | None = None
    links: list[Link | Linkable] = field(default_factory=list)
    memory: int | None = None
    timeout: int | None = None
    environment: dict[str, str] = field(default_factory=dict)
    architecture: AwsArchitecture | None = None
    runtime: AwsLambdaRuntime | None = None
    requirements: str | list[str] | Literal[False] | None = None
    layers: list[Layer] = field(default_factory=list)

    def __post_init__(self) -> None:
        handler_parts = self.handler.split("::")

        if len(handler_parts) > 2:  # noqa: PLR2004
            raise ValueError("Handler can only contain one :: separator")

        if len(handler_parts) == 2:  # noqa: PLR2004
            if self.folder is not None:
                raise ValueError("Cannot specify both 'folder' and use '::' in handler")
            folder_path, handler_path = handler_parts
            if "." in folder_path:
                raise ValueError("Folder path should not contain dots")
        else:
            if self.folder is not None and "." in self.folder:
                raise ValueError("Folder path should not contain dots")
            handler_path = self.handler

        if "." not in handler_path:
            raise ValueError(
                "Handler must contain a dot separator between file path and function name"
            )

        file_path, function_name = handler_path.rsplit(".", 1)
        if not file_path or not function_name:
            raise ValueError("Both file path and function name must be non-empty")

        if "." in file_path:
            raise ValueError("File path part should not contain dots")

        self._validate_requirements()
        self._validate_layers(
            function_runtime=self.runtime or DEFAULT_RUNTIME,
            function_architecture=self.architecture or DEFAULT_ARCHITECTURE,
        )

    def _validate_requirements(self) -> None:
        """Validates the 'requirements' property against allowed types and values."""
        if self.requirements is None:
            return

        if isinstance(self.requirements, str):
            if not self.requirements.strip():
                raise ValueError("If 'requirements' is a string (path), it cannot be empty.")
        elif isinstance(self.requirements, list):
            if not all(isinstance(item, str) for item in (self.requirements)):
                raise TypeError("If 'requirements' is a list, all its elements must be strings.")
        elif isinstance(self.requirements, bool):
            if self.requirements is not False:
                raise ValueError(
                    "If 'requirements' is a boolean, it must be False (to disable). "
                    "True is not allowed."
                )
        else:
            raise TypeError(
                f"'requirements' must be a string (path), list of strings, False, or None. "
                f"Got type: {type(self.requirements).__name__}."
            )

    def _validate_layers(self, function_runtime: str, function_architecture: str) -> None:
        """
        Validates the 'layers' property against types, duplicates, limits,
        and compatibility with the function's runtime/architecture.
        """
        if not self.layers:
            return

        if not isinstance(self.layers, list):
            raise TypeError(
                f"Expected 'layers' to be a list of Layer objects, "
                f"but got {type(self.layers).__name__}."
            )

        if len(self.layers) > MAX_LAMBDA_LAYERS:
            raise ValueError(
                f"A function cannot have more than {MAX_LAMBDA_LAYERS} layers. "
                f"Found {len(self.layers)}."
            )

        layer_names = [layer.name for layer in self.layers if isinstance(layer, Layer)]
        name_counts = Counter(layer_names)
        duplicates = [name for name, count in name_counts.items() if count > 1]
        if duplicates:
            raise ValueError(
                f"Duplicate layer names found: {', '.join(duplicates)}. "
                f"Layer names must be unique for a function."
            )

        for index, layer in enumerate(self.layers):
            if not isinstance(layer, Layer):
                raise TypeError(
                    f"Item at index {index} in 'layers' list is not a Layer instance. "
                    f"Got {type(layer).__name__}."
                )

            layer_runtime = layer.runtime or DEFAULT_RUNTIME
            layer_architecture = layer.architecture or DEFAULT_ARCHITECTURE
            if function_runtime != layer_runtime:
                raise ValueError(
                    f"Function runtime '{function_runtime}' is incompatible "
                    f"with Layer '{layer.name}' runtime '{layer_runtime}'."
                )

            if function_architecture != layer_architecture:
                raise ValueError(
                    f"Function architecture '{function_architecture}' is incompatible "
                    f"with Layer '{layer.name}' architecture '{layer_architecture}'."
                )

    @property
    def folder_path(self) -> str | None:
        return self.folder or (self.handler.split("::")[0] if "::" in self.handler else None)

    @property
    def _handler_part(self) -> str:
        return self.handler.split("::")[1] if "::" in self.handler else self.handler

    @property
    def handler_file_path(self) -> str:
        return self._handler_part.rsplit(".", 1)[0]

    @property
    def local_handler_file_path(self) -> str:
        return self.handler_format.rsplit(".", 1)[0]

    @property
    def handler_function_name(self) -> str:
        return self._handler_part.rsplit(".", 1)[1]

    @property
    def handler_format(self) -> str:
        return self._handler_part if self.folder_path else self.handler.split("/")[-1]

    @property
    def has_only_defaults(self) -> bool:
        ignore_fields = {"handler", "folder"}

        def _field_has_default_value(info: Field) -> bool:
            if info.name in ignore_fields:
                return True

            current_value = getattr(self, info.name)

            if info.default_factory is not MISSING:
                return current_value == info.default_factory()
            if info.default is not MISSING:
                return current_value == info.default
            # Field has no default, so it cannot fail the 'is default' check
            return True

        return all(_field_has_default_value(info) for info in fields(self))
