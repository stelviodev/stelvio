from collections.abc import Callable
from typing import Any

type Customization[T] = T | dict[str, Any] | Callable[[dict[str, Any]], T | dict[str, Any]] | None
type CustomizationNoArgs = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]] | None
# A key whose value this component hands to a component it creates (Cron's `function`). The
# child never reads its parent's app-wide entry, so the key works per instance only and app-wide
# it raises at config load.
type ChildCustomization[T] = T | None
