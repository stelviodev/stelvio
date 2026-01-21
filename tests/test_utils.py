import typing
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

NoneType = type(None)


def assert_config_dict_matches_dataclass(dataclass_type: type, typeddict_type: type) -> None:
    """Tests that a TypedDict matches its corresponding dataclass."""
    # Use get_type_hints for both to resolve forward references consistently
    dataclass_fields = get_type_hints(dataclass_type)
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


def assert_resources_matches_customization_dict(
    resources_type: type,
    customization_dict_type: type,
    *,
    excluded_resource_fields: set[str] | None = None,
) -> None:
    """Tests that a *Resources dataclass has matching keys in *CustomizationDict.

    The *Resources dataclass fields should have corresponding keys in the
    *CustomizationDict TypedDict. This ensures that when new resources are added
    to a component, the customization dict is also updated.

    Note: This checks field name matching only, not type matching, since
    CustomizationDict types include PulumiArgs | dict[str, Any] | None patterns
    while Resources contain actual resource instances.

    Uses __annotations__ directly to avoid issues with forward references
    that are only available under TYPE_CHECKING.

    Args:
        resources_type: The Resources dataclass to check.
        customization_dict_type: The CustomizationDict TypedDict to check.
        excluded_resource_fields: Optional set of field names to exclude from
            the comparison. Use this for fields that are not customizable
            resources (e.g., internal data, computed values).
    """
    excluded = excluded_resource_fields or set()

    # Use __annotations__ directly to avoid forward reference resolution issues
    resources_keys = set(resources_type.__annotations__.keys()) - excluded
    customization_keys = set(customization_dict_type.__annotations__.keys())

    # Check that all resources fields have corresponding customization keys
    missing_in_customization = resources_keys - customization_keys
    assert not missing_in_customization, (
        f"{customization_dict_type.__name__} is missing keys that exist in "
        f"{resources_type.__name__}: {sorted(missing_in_customization)}"
    )

    # Check that all customization keys have corresponding resources fields
    extra_in_customization = customization_keys - resources_keys
    assert not extra_in_customization, (
        f"{customization_dict_type.__name__} has extra keys not in "
        f"{resources_type.__name__}: {sorted(extra_in_customization)}"
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
