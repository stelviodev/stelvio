"""AppSync codegen helper tests — dynamo_get, dynamo_scan, dynamo_put, etc."""

from stelvio.aws.appsync.codegen import (
    dynamo_get,
    dynamo_put,
    dynamo_query,
    dynamo_remove,
    dynamo_scan,
)


def test_dynamo_get_single_key():
    code = dynamo_get("id")
    assert "operation: 'GetItem'" in code
    assert "id: ctx.args.id" in code
    assert "util.dynamodb.toMapValues" in code
    assert "export function request" in code
    assert "export function response" in code


def test_dynamo_get_default_key():
    code = dynamo_get()
    assert "id: ctx.args.id" in code


def test_dynamo_get_compound_key():
    code = dynamo_get(pk="userId", sk="postId")
    assert "userId: ctx.args.userId" in code
    assert "postId: ctx.args.postId" in code
    assert "operation: 'GetItem'" in code


def test_dynamo_put_auto_id():
    code = dynamo_put()
    assert "operation: 'PutItem'" in code
    assert "util.autoId()" in code
    assert "attributeValues: util.dynamodb.toMapValues(ctx.args)" in code


def test_dynamo_put_with_key_fields():
    code = dynamo_put(key_fields=["userId", "postId"])
    assert "operation: 'PutItem'" in code
    assert "userId: ctx.args.userId" in code
    assert "postId: ctx.args.postId" in code
    assert "attributeValues: util.dynamodb.toMapValues(ctx.args)" in code


def test_dynamo_scan_basic():
    code = dynamo_scan()
    assert "operation: 'Scan'" in code
    assert "nextToken: ctx.args.nextToken" in code
    assert "export function request" in code
    assert "export function response" in code


def test_dynamo_scan_with_limit():
    code = dynamo_scan(limit=25)
    assert "limit: 25" in code
    assert "operation: 'Scan'" in code


def test_dynamo_scan_custom_next_token():
    code = dynamo_scan(next_token_arg="cursor")
    assert "ctx.args.cursor" in code


def test_dynamo_query_basic():
    code = dynamo_query("userId")
    assert "operation: 'Query'" in code
    assert "#pk = :pk" in code
    assert '"#pk": "userId"' in code
    assert "ctx.args.userId" in code


def test_dynamo_query_with_sk_condition():
    code = dynamo_query(
        "userId",
        sk_condition="begins_with(#sk, :prefix)",
        sk_expression_names={"#sk": "sortKey"},
        sk_expression_values={":prefix": "prefix"},
    )
    assert "#pk = :pk AND begins_with(#sk, :prefix)" in code
    assert '"#pk": "userId"' in code
    assert '"#sk": "sortKey"' in code
    assert '":prefix": ctx.args.prefix' in code
    assert '":pk": ctx.args.userId' in code


def test_dynamo_query_sk_condition_without_bindings():
    """sk_condition with no extra bindings still works — only pk bindings generated."""
    code = dynamo_query("userId", sk_condition="#sk > :minVal")
    assert "#pk = :pk AND #sk > :minVal" in code
    assert '"#pk": "userId"' in code
    # No extra names/values beyond pk
    assert "ctx.args.userId" in code


def test_dynamo_remove_single_key():
    code = dynamo_remove("id")
    assert "operation: 'DeleteItem'" in code
    assert "id: ctx.args.id" in code


def test_dynamo_remove_default_key():
    code = dynamo_remove()
    assert "id: ctx.args.id" in code


def test_dynamo_remove_compound_key():
    code = dynamo_remove(pk="userId", sk="postId")
    assert "userId: ctx.args.userId" in code
    assert "postId: ctx.args.postId" in code
    assert "operation: 'DeleteItem'" in code


def test_all_codegen_functions_return_valid_js():
    """All codegen functions should return strings containing valid JS function signatures."""
    functions = [
        dynamo_get("id"),
        dynamo_get(pk="pk", sk="sk"),
        dynamo_put(),
        dynamo_put(key_fields=["id"]),
        dynamo_scan(),
        dynamo_scan(limit=10),
        dynamo_query("pk"),
        dynamo_query(
            "pk",
            sk_condition="begins_with(#sk, :val)",
            sk_expression_names={"#sk": "sk"},
            sk_expression_values={":val": "val"},
        ),
        dynamo_remove("id"),
    ]
    for code in functions:
        assert isinstance(code, str)
        assert "export function request" in code
        assert "export function response" in code
        assert "@aws-appsync/utils" in code
