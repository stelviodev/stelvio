import pytest

from stelvio._link_codegen import (
    _pascal_to_snake,
    _to_valid_python_class_name,
)
from stelvio.aws.function.resources_codegen import create_stlv_resource_file_content


@pytest.mark.parametrize(
    ("input_name", "expected"),
    [
        ("testEmail", "TestEmail"),
        ("test-email", "TestEmail"),
        ("test_email", "TestEmail"),
        ("test.email", "TestEmail"),
        ("TestEmail", "TestEmail"),
        ("myAPITest", "MyApiTest"),
        ("XMLParser", "XmlParser"),
        ("simple", "Simple"),
        ("1test", "OneTest"),
        ("myTest-email", "MyTestEmail"),
        ("my-test_email.sender", "MyTestEmailSender"),
    ],
)
def test_to_valid_python_class_name(input_name, expected):
    assert _to_valid_python_class_name(input_name) == expected


@pytest.mark.parametrize(
    ("input_name", "expected"),
    [
        ("TestEmail", "test_email"),
        ("Simple", "simple"),
        ("MyTestEmail", "my_test_email"),
        ("", ""),
    ],
)
def test_pascal_to_snake(input_name, expected):
    assert _pascal_to_snake(input_name) == expected


@pytest.mark.parametrize(
    ("properties", "cors", "message"),
    [
        (
            {"my-bucket": ["arn"], "my_bucket": ["arn"]},
            False,
            "both generate Python name 'my_bucket'",
        ),
        ({"class": ["arn"]}, False, "invalid or reserved Python name 'class'"),
        ({"---": ["arn"]}, False, "invalid or reserved Python name ''"),
        ({"bucket": ["not-valid"]}, False, "Property 'not-valid'"),
        ({"bucket": ["class"]}, False, "Property 'class'"),
        ({"bucket": ["__dict__"]}, False, "Property '__dict__'"),
        ({"bucket": ["cached_property", "arn"]}, False, "conflicts with a generated name"),
        ({"cors": ["arn"]}, True, "conflicts with reserved Python name 'cors'"),
        (
            {"bucket": ["url", "URL"]},
            False,
            "both generate environment variable 'STLV_BUCKET_URL'",
        ),
        (
            {"a": ["b_url"], "a-b": ["url"]},
            False,
            "both generate environment variable 'STLV_A_B_URL'",
        ),
    ],
)
def test_link_accessor_generation_rejects_ambiguous_and_reserved_names(properties, cors, message):
    with pytest.raises(ValueError, match=message):
        create_stlv_resource_file_content(properties, include_cors=cors)
