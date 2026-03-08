"""AppSync codegen helper tests — dynamo_get, dynamo_scan, dynamo_put, etc."""

import pytest

from stelvio.aws.appsync.codegen import (
    dynamo_get,
    dynamo_put,
    dynamo_query,
    dynamo_remove,
    dynamo_scan,
)


def assert_js_mapping_template(code: str, *, operation: str, fragments: list[str]) -> None:
    assert isinstance(code, str)
    assert "export function request" in code
    assert "export function response" in code
    assert "@aws-appsync/utils" in code
    assert f"operation: '{operation}'" in code
    for fragment in fragments:
        assert fragment in code


@pytest.mark.parametrize(
    "fn",
    [dynamo_get, dynamo_remove],
    ids=["get", "remove"],
)
def test_explicit_id_key_matches_default(fn):
    """Passing explicit pk='id' produces same output as default."""
    assert fn("id") == fn()


@pytest.mark.parametrize(
    ("fn", "operation", "kwargs", "fragments"),
    [
        (dynamo_get, "GetItem", {}, ["id: ctx.args.id"]),
        (
            dynamo_get,
            "GetItem",
            {"pk": "userId", "sk": "postId"},
            ["userId: ctx.args.userId", "postId: ctx.args.postId"],
        ),
        (dynamo_remove, "DeleteItem", {}, ["id: ctx.args.id"]),
        (
            dynamo_remove,
            "DeleteItem",
            {"pk": "userId", "sk": "postId"},
            ["userId: ctx.args.userId", "postId: ctx.args.postId"],
        ),
    ],
    ids=["get-default", "get-compound", "remove-default", "remove-compound"],
)
def test_dynamo_get_remove_variants(fn, operation, kwargs, fragments):
    assert_js_mapping_template(fn(**kwargs), operation=operation, fragments=fragments)


@pytest.mark.parametrize(
    ("kwargs", "fragments"),
    [
        ({}, ["key: util.dynamodb.toMapValues({ id: util.autoId() })"]),
        (
            {"key_fields": ["userId", "postId"]},
            ["userId: ctx.args.userId", "postId: ctx.args.postId"],
        ),
    ],
    ids=["auto-id", "compound-key"],
)
def test_dynamo_put_variants(kwargs, fragments):
    result = dynamo_put(**kwargs)
    assert_js_mapping_template(
        result,
        operation="PutItem",
        fragments=["attributeValues: util.dynamodb.toMapValues(ctx.args)", *fragments],
    )


@pytest.mark.parametrize(
    ("kwargs", "expected_fragments", "unexpected_fragments"),
    [
        ({}, ["nextToken: ctx.args.nextToken"], ["limit"]),
        ({"limit": 25}, ["limit: 25"], []),
        ({"next_token_arg": "cursor"}, ["nextToken: ctx.args.cursor"], []),
    ],
    ids=["basic", "with-limit", "custom-next-token"],
)
def test_dynamo_scan_variants(kwargs, expected_fragments, unexpected_fragments):
    result = dynamo_scan(**kwargs)
    assert "operation: 'Scan'" in result
    for fragment in expected_fragments:
        assert fragment in result
    for fragment in unexpected_fragments:
        assert fragment not in result


@pytest.mark.parametrize(
    ("factory", "fragments"),
    [
        (
            lambda: dynamo_query("userId"),
            [
                "expression: '#pk = :pk'",
                'expressionNames: {"#pk": "userId"}',
                '":pk": ctx.args.userId',
            ],
        ),
        (
            lambda: dynamo_query("userId", sk_condition="begins_with(sk, :prefix)"),
            [
                "expression: '#pk = :pk AND begins_with(sk, :prefix)'",
                '":pk": ctx.args.userId',
            ],
        ),
        (
            lambda: dynamo_query(
                "userId",
                sk_condition="begins_with(sk, :prefix)",
                sk_expression_values={":prefix": "ctx.args.prefix"},
            ),
            [
                "expression: '#pk = :pk AND begins_with(sk, :prefix)'",
                '":pk": ctx.args.userId',
                '":prefix": ctx.args.prefix',
            ],
        ),
    ],
    ids=["basic", "sk-condition", "sk-condition-expression-values"],
)
def test_dynamo_query_variants(factory, fragments):
    assert_js_mapping_template(factory(), operation="Query", fragments=fragments)
