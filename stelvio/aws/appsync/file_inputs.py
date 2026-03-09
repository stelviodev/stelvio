from pathlib import Path

from stelvio.project import get_project_root

_SCHEMA_EXTENSIONS = frozenset({".graphql", ".gql"})
_JS_EXTENSIONS = frozenset({".js"})


def _read_file_by_extension(
    value: str,
    *,
    valid_extensions: frozenset[str],
    context_label: str,
) -> str:
    normalized_value = value.strip()
    extension = Path(normalized_value).suffix.lower()
    if extension not in valid_extensions:
        return value

    file_path = Path(get_project_root()) / normalized_value
    if not file_path.is_file():
        raise FileNotFoundError(
            f"Expected {context_label} file '{normalized_value}' under project root "
            f"'{get_project_root()}', but it does not exist."
        )

    return file_path.read_text()


def read_schema_input(value: str) -> str:
    return _read_file_by_extension(
        value,
        valid_extensions=_SCHEMA_EXTENSIONS,
        context_label="schema",
    )


def read_js_code_input(value: str) -> str:
    return _read_file_by_extension(
        value,
        valid_extensions=_JS_EXTENSIONS,
        context_label="code",
    )
