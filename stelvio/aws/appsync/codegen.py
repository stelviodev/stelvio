"""Code generation helpers for common AppSync resolver patterns.

Pure functions returning APPSYNC_JS code strings for use with code= parameter.
"""


def _key_mapping(fields: dict[str, str]) -> str:
    """Build the key mapping entries for DynamoDB resolver code."""
    entries = [f"            {k}: ctx.args.{v}," for k, v in fields.items()]
    return "\n".join(entries)


def _parse_key_args(pk: str, sk: str | None) -> dict[str, str]:
    """Parse primary key and optional sort key into a field mapping."""
    keys = {pk: pk}
    if sk is not None:
        keys[sk] = sk
    return keys


def _expression_values_mapping(expression_values: dict[str, str]) -> str:
    entries = [
        f'                "{placeholder}": {value},'
        for placeholder, value in expression_values.items()
    ]
    return "\n".join(entries)


def dynamo_get(pk: str = "id", sk: str | None = None) -> str:
    """Generate APPSYNC_JS code for a DynamoDB GetItem operation.

    Args:
        pk: Partition key field name (default "id"). Used as both the DynamoDB
            attribute name and the GraphQL argument name.
        sk: Sort key field name. If provided, creates a compound key lookup.
    """
    keys = _parse_key_args(pk, sk)
    return f"""\
import {{ util }} from '@aws-appsync/utils';

export function request(ctx) {{
    return {{
        operation: 'GetItem',
        key: util.dynamodb.toMapValues({{
{_key_mapping(keys)}
        }}),
    }};
}}

export function response(ctx) {{
    return ctx.result;
}}
"""


def dynamo_put(key_fields: list[str] | None = None) -> str:
    """Generate APPSYNC_JS code for a DynamoDB PutItem operation.

    Args:
        key_fields: List of key field names to extract from args for the key.
            If None, the entire args object is used as the item with auto-generated ID.
    """
    if key_fields:
        keys = {k: k for k in key_fields}
        return f"""\
import {{ util }} from '@aws-appsync/utils';

export function request(ctx) {{
    return {{
        operation: 'PutItem',
        key: util.dynamodb.toMapValues({{
{_key_mapping(keys)}
        }}),
        attributeValues: util.dynamodb.toMapValues(ctx.args),
    }};
}}

export function response(ctx) {{
    return ctx.result;
}}
"""
    return """\
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


def dynamo_scan(limit: int | None = None, next_token_arg: str = "nextToken") -> str:  # noqa: S107
    """Generate APPSYNC_JS code for a DynamoDB Scan operation.

    Args:
        limit: Maximum number of items to return per page.
        next_token_arg: GraphQL argument name for pagination token.
    """
    limit_line = f"\n        limit: {limit}," if limit is not None else ""
    return f"""\
import {{ util }} from '@aws-appsync/utils';

export function request(ctx) {{
    return {{
        operation: 'Scan',{limit_line}
        nextToken: ctx.args.{next_token_arg},
    }};
}}

export function response(ctx) {{
    return ctx.result;
}}
"""


def dynamo_query(
    pk_field: str,
    sk_condition: str | None = None,
    sk_expression_values: dict[str, str] | None = None,
) -> str:
    """Generate APPSYNC_JS code for a DynamoDB Query operation.

    Args:
        pk_field: Partition key field name for the equality condition.
        sk_condition: Optional sort key condition expression
            (e.g., "begins_with(sk, :prefix)").
        sk_expression_values: Optional expression value placeholders used by
            sk_condition, mapping placeholder names to JavaScript expressions
            (e.g., {":prefix": "ctx.args.prefix"}).
    """
    expression = "#pk = :pk"
    expression_names = f'{{"#pk": "{pk_field}"}}'
    expression_values = {":pk": f"ctx.args.{pk_field}"}
    if sk_expression_values:
        expression_values.update(sk_expression_values)

    if sk_condition:
        expression = f"#pk = :pk AND {sk_condition}"

    return f"""\
import {{ util }} from '@aws-appsync/utils';

export function request(ctx) {{
    return {{
        operation: 'Query',
        query: {{
            expression: '{expression}',
            expressionNames: {expression_names},
            expressionValues: util.dynamodb.toMapValues({{
{_expression_values_mapping(expression_values)}
            }}),
        }},
    }};
}}

export function response(ctx) {{
    return ctx.result;
}}
"""


def dynamo_remove(pk: str = "id", sk: str | None = None) -> str:
    """Generate APPSYNC_JS code for a DynamoDB DeleteItem operation.

    Args:
        pk: Partition key field name (default "id").
        sk: Sort key field name. If provided, creates a compound key delete.
    """
    keys = _parse_key_args(pk, sk)
    return f"""\
import {{ util }} from '@aws-appsync/utils';

export function request(ctx) {{
    return {{
        operation: 'DeleteItem',
        key: util.dynamodb.toMapValues({{
{_key_mapping(keys)}
        }}),
    }};
}}

export function response(ctx) {{
    return ctx.result;
}}
"""
