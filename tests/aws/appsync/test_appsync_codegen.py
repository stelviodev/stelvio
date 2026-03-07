"""AppSync codegen helper tests — dynamo_get, dynamo_scan, dynamo_put, etc."""

from stelvio.aws.appsync.codegen import (
    dynamo_get,
    dynamo_put,
    dynamo_query,
    dynamo_remove,
    dynamo_scan,
)


def test_dynamo_get_single_key():
    expected = dynamo_get()  # default pk="id"
    assert dynamo_get("id") == expected


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
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'GetItem',
        key: util.dynamodb.toMapValues({
            userId: ctx.args.userId,
            postId: ctx.args.postId,
        }),
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_get(pk="userId", sk="postId") == expected


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
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'PutItem',
        key: util.dynamodb.toMapValues({
            userId: ctx.args.userId,
            postId: ctx.args.postId,
        }),
        attributeValues: util.dynamodb.toMapValues(ctx.args),
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_put(key_fields=["userId", "postId"]) == expected


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
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'Scan',
        limit: 25,
        nextToken: ctx.args.nextToken,
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_scan(limit=25) == expected


def test_dynamo_scan_custom_next_token():
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'Scan',
        nextToken: ctx.args.cursor,
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_scan(next_token_arg="cursor") == expected  # noqa: S106


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
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'Query',
        query: {
            expression: '#pk = :pk AND begins_with(sk, :prefix)',
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
    assert dynamo_query("userId", sk_condition="begins_with(sk, :prefix)") == expected


def test_dynamo_query_with_sk_condition_expression_values():
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'Query',
        query: {
            expression: '#pk = :pk AND begins_with(sk, :prefix)',
            expressionNames: {"#pk": "userId"},
            expressionValues: util.dynamodb.toMapValues({
                ":pk": ctx.args.userId,
                ":prefix": ctx.args.prefix,
            }),
        },
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert (
        dynamo_query(
            "userId",
            sk_condition="begins_with(sk, :prefix)",
            sk_expression_values={":prefix": "ctx.args.prefix"},
        )
        == expected
    )


def test_dynamo_remove_single_key():
    expected = dynamo_remove()  # default pk="id"
    assert dynamo_remove("id") == expected


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
    expected = """\
import { util } from '@aws-appsync/utils';

export function request(ctx) {
    return {
        operation: 'DeleteItem',
        key: util.dynamodb.toMapValues({
            userId: ctx.args.userId,
            postId: ctx.args.postId,
        }),
    };
}

export function response(ctx) {
    return ctx.result;
}
"""
    assert dynamo_remove(pk="userId", sk="postId") == expected


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
