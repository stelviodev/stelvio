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


def create_stlv_resource_file_content(
    link_properties_map: dict[str, list[str]], include_cors: bool = False
) -> str | None:
    """Generate resource access file content with classes for linked resources."""
    # Return None if no properties to generate and no CORS
    if not any(link_properties_map.values()) and not include_cors:
        return None

    lines = [
        "import os",
        "from dataclasses import dataclass",
        "from typing import Final",
        "from functools import cached_property\n\n",
    ]

    # Generate CORS class if needed
    if include_cors:
        lines.extend(_create_cors_class())

    for link_name, properties in link_properties_map.items():
        if not properties:
            continue
        lines.extend(_create_link_resource_class(link_name, properties))

    lines.extend(["@dataclass(frozen=True)", "class LinkedResources:"])

    # Add cors to LinkedResources if included
    if include_cors:
        lines.append("    cors: Final[CorsResource] = CorsResource()")

    for link_name in link_properties_map:
        cls_name = _to_valid_python_class_name(link_name)
        lines.append(
            f"    {_pascal_to_snake(cls_name)}: Final[{cls_name}Resource] = {cls_name}Resource()"
        )
    lines.extend(["\n", "Resources: Final = LinkedResources()"])

    return "\n".join(lines)


def _create_cors_class() -> list[str]:
    """Generate CORS resource class with env vars and get_headers() helper."""
    return [
        "@dataclass(frozen=True)",
        "class CorsResource:",
        "    @cached_property",
        "    def allow_origin(self) -> str:",
        '        return os.environ.get("STLV_CORS_ALLOW_ORIGIN", "")',
        "",
        "    @cached_property",
        "    def expose_headers(self) -> str:",
        '        return os.environ.get("STLV_CORS_EXPOSE_HEADERS", "")',
        "",
        "    @cached_property",
        "    def allow_credentials(self) -> bool:",
        '        return os.environ.get("STLV_CORS_ALLOW_CREDENTIALS", "false") == "true"',
        "",
        "    def get_headers(self) -> dict[str, str]:",
        '        """Returns CORS headers for API Gateway responses."""',
        '        headers = {"Access-Control-Allow-Origin": self.allow_origin}',
        "        if self.expose_headers:",
        '            headers["Access-Control-Expose-Headers"] = self.expose_headers',
        "        if self.allow_credentials:",
        '            headers["Access-Control-Allow-Credentials"] = "true"',
        "        return headers",
        "",
        "",
    ]


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


def _pascal_to_snake(pascal_str: str) -> str:
    return "".join(
        "_" + char.lower() if char.isupper() and i > 0 else char.lower()
        for i, char in enumerate(pascal_str)
    )
