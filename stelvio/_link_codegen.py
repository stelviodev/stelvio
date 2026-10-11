"""Shared Python names for generated link accessors."""

import keyword
import re
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet

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


def _to_valid_python_class_name(name: str) -> str:
    words = name.replace("-", " ").replace(".", " ").replace("_", " ").split()
    parts = []
    for word in words:
        split = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z][a-z]|\d|\W|$)|\d+", word)
        parts.extend(split or [word])
    class_name = "".join("".join(c for c in part if c.isalnum()).capitalize() for part in parts)
    if class_name and class_name[0].isdigit():
        class_name = NUMBER_WORDS[class_name[0]] + class_name[1:]
    return class_name


def _pascal_to_snake(name: str) -> str:
    return "".join(
        "_" + char.lower() if char.isupper() and i > 0 else char.lower()
        for i, char in enumerate(name)
    )


def _validate_identifier(name: str, *, origin: str) -> None:
    if not isinstance(name, str):
        raise TypeError(f"{origin} must be a string, got {type(name).__name__}.")
    if not name.isidentifier() or keyword.iskeyword(name) or name.startswith("__"):
        raise ValueError(f"{origin} produces invalid or reserved Python name {name!r}.")


def _link_names(
    properties: Mapping[str, Sequence[str]],
    *,
    class_suffix: str,
    reserved_classes: AbstractSet[str] = frozenset(),
    reserved_attributes: AbstractSet[str] = frozenset(),
    reserved_properties: AbstractSet[str] = frozenset(),
) -> dict[str, tuple[str, str]]:
    names = {}
    classes: dict[str, str] = {}
    attributes: dict[str, str] = {}
    for name, fields in properties.items():
        if not fields:
            continue
        base = _to_valid_python_class_name(name)
        attribute = _pascal_to_snake(base)
        class_name = base + class_suffix
        _validate_identifier(attribute, origin=f"Link {name!r}")
        _validate_identifier(class_name, origin=f"Link {name!r}")
        for generated, used, reserved in (
            (attribute, attributes, reserved_attributes),
            (class_name, classes, reserved_classes),
        ):
            if generated in reserved:
                raise ValueError(
                    f"Link {name!r} conflicts with reserved Python name {generated!r}."
                )
            if generated in used:
                raise ValueError(
                    f"Links {used[generated]!r} and {name!r} "
                    f"both generate Python name {generated!r}."
                )
            used[generated] = name
        for field in fields:
            _validate_identifier(field, origin=f"Property {field!r} of link {name!r}")
            if field in reserved_properties:
                raise ValueError(
                    f"Property {field!r} of link {name!r} conflicts with a generated name."
                )
        names[name] = (class_name, attribute)
    return names
