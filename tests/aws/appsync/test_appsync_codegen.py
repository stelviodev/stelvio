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
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'GetItem',
        key: util.dynamodb.toMapValues({
            id: ctx.args.id,
        }),
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_get() == expected


def test_dynamo_get_compound_key():
    code = dynamo_get(pk="userId", sk="postId")
    assert "userId: ctx.args.userId" in code
    assert "postId: ctx.args.postId" in code
    assert "operation: 'GetItem'" in code


def test_dynamo_put_auto_id():
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'PutItem',
        key: util.dynamodb.toMapValues({ id: util.autoId() }),
        attributeValues: util.dynamodb.toMapValues(ctx.args),
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_put() == expected


def test_dynamo_put_with_key_fields():
    code = dynamo_put(key_fields=["userId", "postId"])
    assert "operation: 'PutItem'" in code
    assert "userId: ctx.args.userId" in code
    assert "postId: ctx.args.postId" in code
    assert "attributeValues: util.dynamodb.toMapValues(ctx.args)" in code


def test_dynamo_scan_basic():
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'Scan',
        nextToken: ctx.args.nextToken,
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_scan() == expected


def test_dynamo_scan_with_limit():
    code = dynamo_scan(limit=25)
    assert "limit: 25" in code
    assert "operation: 'Scan'" in code


def test_dynamo_scan_custom_next_token():
    code = dynamo_scan(next_token_arg="cursor")
    assert "ctx.args.cursor" in code


def test_dynamo_query_basic():
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'Query',
        query: {
            expression: '#pk = :pk',
            expressionNames: {"#pk": "userId"},
            expressionValues: util.dynamodb.toMapValues({
                ":pk": ctx.args.userId,
            }),
        },
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_query("userId") == expected


def test_dynamo_query_with_sk_condition():
    code = dynamo_query("userId", sk_condition="begins_with(sk, :prefix)")
    assert "#pk = :pk AND begins_with(sk, :prefix)" in code


def test_dynamo_query_with_sk_condition_expression_values():
    code = dynamo_query(
        "userId",
        sk_condition="begins_with(sk, :prefix)",
        sk_expression_values={":prefix": "ctx.args.prefix"},
    )
    assert '":pk": ctx.args.userId' in code
    assert '":prefix": ctx.args.prefix' in code


def test_dynamo_remove_single_key():
    code = dynamo_remove("id")
    assert "operation: 'DeleteItem'" in code
    assert "id: ctx.args.id" in code


def test_dynamo_remove_default_key():
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'DeleteItem',
        key: util.dynamodb.toMapValues({
            id: ctx.args.id,
        }),
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_remove() == expected


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
        dynamo_remove("id"),
    ]
    for code in functions:
        assert isinstance(code, str)
        assert "export function request" in code
        assert "export function response" in code
        assert "@aws-appsync/utils" in code
