from collections.abc import Callable
from typing import Any

type Customization[T] = T | dict[str, Any] | Callable[[dict[str, Any]], T | dict[str, Any]] | None
type CustomizationNoArgs = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] | None
