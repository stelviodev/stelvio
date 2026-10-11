from pathlib import Path

from stelvio._link_codegen import _link_names, _to_valid_python_class_name

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
    link_properties_map: dict[str, list[str]],
    include_cors: bool = False,
    *,
    validate_environment_names: bool = True,
) -> str | None:
    """Generate resource access file content with classes for linked resources."""
    # Return None if no properties to generate and no CORS
    if not any(link_properties_map.values()) and not include_cors:
        return None

    names = _link_names(
        link_properties_map,
        class_suffix="Resource",
        reserved_classes={"LinkedResources", "CorsResource"}
        if include_cors
        else {"LinkedResources"},
        reserved_attributes={"cors"} if include_cors else set(),
        reserved_properties={"cached_property"},
    )
    if validate_environment_names:
        _validate_env_names(link_properties_map)

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

    for link_name, properties in link_properties_map.items():
        if not properties:
            continue
        cls_name, attribute = names[link_name]
        lines.append(f"    {attribute}: Final[{cls_name}] = {cls_name}()")
    lines.extend(["\n", "Resources: Final = LinkedResources()"])

    return "\n".join(lines)


def _validate_env_names(link_properties_map: dict[str, list[str]]) -> None:
    seen: dict[str, tuple[str, str]] = {}
    for name, properties in link_properties_map.items():
        for prop in properties:
            key = _envar_name(name, prop)
            origin = (name, prop)
            if key in seen and seen[key] != origin:
                other_name, other_prop = seen[key]
                raise ValueError(
                    f"Link properties {other_name!r}.{other_prop} and {name!r}.{prop} "
                    f"both generate environment variable {key!r}."
                )
            seen[key] = origin


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
                f'        return os.environ["{_envar_name(link_name, prop)}"]\n',
            ]
        )
    lines.append("")
    return lines
