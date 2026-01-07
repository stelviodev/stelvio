import pytest

from stelvio.aws.function.resources_codegen import (
    _pascal_to_snake,
    _to_valid_python_class_name,
)


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
