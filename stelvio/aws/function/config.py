from collections import Counter
from dataclasses import MISSING, Field, dataclass, field, fields
from typing import Literal, TypedDict

from stelvio.aws.cors import CorsConfig, CorsConfigDict
from stelvio.aws.function.constants import DEFAULT_ARCHITECTURE, DEFAULT_RUNTIME, MAX_LAMBDA_LAYERS
from stelvio.aws.layer import Layer
from stelvio.aws.types import AwsArchitecture, AwsLambdaRuntime
from stelvio.link import Link, Linkable


class FunctionUrlConfigDict(TypedDict, total=False):
    auth: Literal["default", "iam"] | None
    cors: bool | CorsConfig | CorsConfigDict | None
    streaming: bool


@dataclass(frozen=True, kw_only=True)
class FunctionUrlConfig:
    auth: Literal["default", "iam"] | None = "default"
    cors: bool | CorsConfig | CorsConfigDict | None = None
    streaming: bool = False

    def __post_init__(self) -> None:
        # Validate auth
        if self.auth is not None and self.auth not in ("default", "iam"):
            raise ValueError(f"Invalid auth value: {self.auth}. Must be 'default', 'iam', or None")

        # Validate streaming
        if not isinstance(self.streaming, bool):
            raise TypeError("streaming must be a boolean")

    @property
    def normalized_cors(self) -> CorsConfig | None:
        """Normalize CORS configuration to CorsConfig or None.

        Converts:
        - True → CorsConfig with permissive defaults
        - CorsConfig → returns as-is
        - dict (CorsConfigDict) → CorsConfig(**dict) with validation
        - False or None → None (CORS disabled)
        """
        if self.cors is True:
            return CorsConfig(allow_origins="*", allow_headers="*", allow_methods="*")
        if isinstance(self.cors, CorsConfig):
            return self.cors
        if isinstance(self.cors, dict):
            return CorsConfig(**self.cors)
        return None


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
    url: Literal["public", "private"] | FunctionUrlConfig | FunctionUrlConfigDict | None


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
    url: Literal["public", "private"] | FunctionUrlConfig | FunctionUrlConfigDict | None = None

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
        self._validate_url()

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

    def _validate_url(self) -> None:
        """Validates the 'url' property against allowed types and values."""
        if self.url is None:
            return

        if isinstance(self.url, str):
            if self.url not in ("public", "private"):
                raise ValueError(
                    f"Invalid url shortcut: '{self.url}'. Must be 'public' or 'private'"
                )
        elif isinstance(self.url, FunctionUrlConfig):
            # FunctionUrlConfig validates itself in __post_init__
            pass
        elif isinstance(self.url, dict):
            # Validate dict can be converted to FunctionUrlConfig
            try:
                FunctionUrlConfig(**self.url)
            except (TypeError, ValueError) as e:
                raise ValueError(f"Invalid url configuration: {e}") from e
        else:
            raise TypeError(
                f"url must be 'public', 'private', FunctionUrlConfig, dict, or None. "
                f"Got {type(self.url).__name__}"
            )

    @property
    def folder_path(self) -> str | None:
        """Returns the folder containing the handler code.

        For "functions/orders::handler.process" → "functions/orders"
        For "handler.process" with folder="functions/orders" → "functions/orders"
        For "functions/users.process" without folder → None
        """
        return self.folder or (self.handler.split("::")[0] if "::" in self.handler else None)

    @property
    def full_handler_path(self) -> str:
        """Returns the full handler path within the project including function name.

        For "functions/orders::handler.process" → "functions/orders/handler.process"
        For "handler.process" with folder="functions/orders" → "functions/orders/handler.process"
        For "functions/users.process" without folder → "functions/users.process"
        """
        if self.folder_path:
            return f"{self.folder_path}/{self._handler_part}"
        return self._handler_part

    @property
    def full_handler_python_path(self) -> str:
        """
        Returns the full path to the handler Python file.

        For "functions/orders::handler.process" → "functions/orders/handler.py"
        For "handler.process" with folder="functions/orders" → "functions/orders/handler.py"
        For "functions/users.process" without folder → "functions/users.py"
        """
        return self.full_handler_path.rsplit(".")[0] + ".py"

    @property
    def _handler_part(self) -> str:
        """Returns the handler string without the folder prefix.

        For "api::orders/handler.process" → "orders/handler.process"
        For "orders/handler.process" → "orders/handler.process"
        """
        return self.handler.split("::")[1] if "::" in self.handler else self.handler

    @property
    def handler_file_path(self) -> str:
        """Returns the file path portion of the handler (without function name).

        For "api::orders/handler.process" → "orders/handler"
        For "handler.process" → "handler"
        """
        return self._handler_part.rsplit(".", 1)[0]

    @property
    def local_handler_file_path(self) -> str:
        """Returns the file path as it appears in the Lambda package.

        For "api::orders/handler.process" → "orders/handler"
        For "orders/handler.process" → "handler" (last segment only)
        """
        return self.handler_format.rsplit(".", 1)[0]

    @property
    def handler_function_name(self) -> str:
        """Returns the function name portion of the handler.

        For "api::orders/handler.process" → "process"
        For "handler.process" → "process"
        """
        return self._handler_part.rsplit(".", 1)[1]

    @property
    def handler_format(self) -> str:
        """Returns the handler string in AWS Lambda format.

        For "api::orders/handler.process" → "orders/handler.process"
        For "orders/handler.process" → "handler.process" (last segment only)
        """
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
