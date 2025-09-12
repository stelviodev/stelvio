import pytest

from stelvio.aws.dynamo_db import DynamoTableConfig, FieldType, StreamView


def test_stream_config_properties():
    """Test stream configuration properties without Pulumi."""
    # Test enum value
    config_enum = DynamoTableConfig(
        fields={"id": FieldType.STRING}, partition_key="id", stream=StreamView.KEYS_ONLY
    )
    assert config_enum.stream_enabled is True
    assert config_enum.normalized_stream_view_type == "KEYS_ONLY"

    # Test string literal
    config_literal = DynamoTableConfig(
        fields={"id": FieldType.STRING}, partition_key="id", stream="new-image"
    )
    assert config_literal.stream_enabled is True
    assert config_literal.normalized_stream_view_type == "NEW_IMAGE"

    # Test no stream
    config_no_stream = DynamoTableConfig(fields={"id": FieldType.STRING}, partition_key="id")
    assert config_no_stream.stream_enabled is False
    assert config_no_stream.normalized_stream_view_type is None

    # Test all stream types
    stream_mappings = [
        ("keys-only", "KEYS_ONLY"),
        ("new-image", "NEW_IMAGE"),
        ("old-image", "OLD_IMAGE"),
        ("new-and-old-images", "NEW_AND_OLD_IMAGES"),
    ]

    for literal, expected_aws_value in stream_mappings:
        config = DynamoTableConfig(
            fields={"id": FieldType.STRING}, partition_key="id", stream=literal
        )
        assert config.normalized_stream_view_type == expected_aws_value


def test_field_type_literals_normalized():
    """Test that field type literals are normalized correctly."""
    config = DynamoTableConfig(
        fields={"id": "string", "score": "number", "data": "binary"}, partition_key="id"
    )
    assert config.normalized_fields == {"id": "S", "score": "N", "data": "B"}

    # Test with mixed types
    config2 = DynamoTableConfig(
        fields={"id": FieldType.STRING, "score": "number", "data": "B"}, partition_key="id"
    )
    assert config2.normalized_fields == {"id": "S", "score": "N", "data": "B"}


@pytest.mark.parametrize(
    ("config_args", "expected_error"),
    [
        (
            {"fields": {"id": FieldType.STRING}, "partition_key": "invalid_key"},
            "partition_key 'invalid_key' not in fields list",
        ),
        (
            {
                "fields": {"id": FieldType.STRING},
                "partition_key": "id",
                "sort_key": "invalid_sort",
            },
            "sort_key 'invalid_sort' not in fields list",
        ),
    ],
)
def test_dynamo_table_config_validation_basic(config_args, expected_error):
    """Test basic validation of DynamoTableConfig."""
    with pytest.raises(ValueError, match=expected_error):
        DynamoTableConfig(**config_args)
