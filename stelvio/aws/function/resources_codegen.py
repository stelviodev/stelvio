from pathlib import Path

from .constants import NUMBER_WORDS
from .naming import _envar_name


def _create_stlv_resource_file(folder: Path, content: str | None) -> None:
    """Create resource access file with supplied content."""
    path = folder / "stlv_resources.py"
    # Delete file if no content
    if not content:
        path.unlink(missing_ok=True)
        return
    with Path.open(path, "w") as f:
        f.write(content)


def create_stlv_resource_file_content(link_properties_map: dict[str, list[str]]) -> str | None:
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
        cls_name = _to_valid_python_class_name(link_name)
        lines.append(
            f"    {_pascal_to_camel(cls_name)}: Final[{cls_name}Resource] = {cls_name}Resource()"
        )
    lines.extend(["\n", "Resources: Final = LinkedResources()"])

    return "\n".join(lines)


def _create_link_resource_class(link_name: str, properties: list[str]) -> list[str] | None:
    if not properties:
        return None
    class_name = _to_valid_python_class_name(link_name)
    lines = [
        "@dataclass(frozen=True)",
        f"class {class_name}Resource:",
    ]
    for prop in properties:
        lines.extend(
            [
                "    @cached_property",
                f"    def {prop}(self) -> str:",
                f'        return os.getenv("{_envar_name(link_name, prop)}")\n',
            ]
        )
    lines.append("")
    return lines


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
