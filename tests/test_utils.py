import typing
from dataclasses import fields
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

NoneType = type(None)


def assert_config_dict_matches_dataclass(dataclass_type: type, typeddict_type: type) -> None:
    """Tests that a TypedDict matches its corresponding dataclass."""
    # noinspection PyTypeChecker
    dataclass_fields = {f.name: f.type for f in fields(dataclass_type)}
    typeddict_fields = get_type_hints(typeddict_type)

    assert set(dataclass_fields.keys()) == set(typeddict_fields.keys()), (
        f"{typeddict_type.__name__} and {dataclass_type.__name__} have different fields."
    )

    for field_name, dataclass_field_type in dataclass_fields.items():
        if field_name not in typeddict_fields:
            continue

        typeddict_field_type = typeddict_fields[field_name]

        normalized_dataclass_type = normalize_type(dataclass_field_type)
        normalized_typeddict_type = normalize_type(typeddict_field_type)

        assert normalized_dataclass_type == normalized_typeddict_type, (
            f"Type mismatch for field '{field_name}' in {dataclass_type.__name__}:\n"
            f"  Dataclass (original): {dataclass_field_type}\n"
            f"  TypedDict (original): {typeddict_field_type}\n"
            f"  Dataclass (normalized): {normalized_dataclass_type}\n"
            f"  TypedDict (normalized): {normalized_typeddict_type}\n"
            f"  Comparison Failed: {normalized_dataclass_type} != {normalized_typeddict_type}"
        )


def normalize_type(type_hint: type) -> type:
    """
    Normalizes a type hint by removing 'NoneType' from its Union representation,
    if applicable. Keeps other Union members intact.

    Examples:
        Union[str, None]          -> str
        Union[str, list[str], None] -> Union[str, list[str]]
        Union[Literal["a", "b"], None] -> Literal["a", "b"]
        str                       -> str
        Union[str, int]           -> Union[str, int]
        NoneType                  -> NoneType
        Union[NoneType]           -> NoneType
    """
    origin = get_origin(type_hint)

    if origin is Union or origin is UnionType:
        args = get_args(type_hint)

        non_none_args = tuple(arg for arg in args if arg is not NoneType)

        if not non_none_args:
            return NoneType
        if len(non_none_args) == 1:
            return non_none_args[0]
        return typing.Union[non_none_args]  # noqa: UP007

    return type_hint
