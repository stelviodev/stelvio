from stelvio.aws.permission import AwsPermission


def test_init_with_simple_values():
    actions = ["s3:GetObject", "s3:PutObject"]
    resources = ["arn:aws:s3:::my-bucket/*"]

    permission = AwsPermission(actions=actions, resources=resources)

    assert permission.actions == actions
    assert permission.resources == resources


def test_init_with_single_string():
    permission = AwsPermission(
        actions="dynamodb:GetItem",
        resources="arn:aws:dynamodb:us-east-1:123456789012:table/my-table",
    )

    assert permission.actions == "dynamodb:GetItem"
    assert permission.resources == "arn:aws:dynamodb:us-east-1:123456789012:table/my-table"


def test_to_provider_format():
    actions = ["dynamodb:GetItem", "dynamodb:PutItem"]
    resources = ["arn:aws:dynamodb:us-east-1:123456789012:table/my-table"]

    permission = AwsPermission(actions=actions, resources=resources)
    provider_format = permission.to_provider_format()

    # We can't check the type directly since GetPolicyDocumentStatementArgsDict is a TypedDict
    # Instead check that it's a dict with the expected structure
    assert isinstance(provider_format, dict)

    # Check that values are passed through correctly
    assert provider_format["actions"] == actions
    assert provider_format["resources"] == resources
