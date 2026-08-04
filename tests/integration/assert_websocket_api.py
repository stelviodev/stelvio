"""Boto3 read-back assertions for WebsocketApi deployed resources."""

from __future__ import annotations

import json

from .assert_helpers import _boto3_session


def assert_websocket_api(
    api_id: str,
    *,
    expected_route_keys: set[str],
    expected_integration_count: int | None = None,
) -> None:
    """Assert a WebSocket API's protocol, routes, and Lambda integrations.

    Integration count is not assumed equal to route count: shared handlers
    intentionally create one integration for many routes.
    """
    client = _boto3_session().client("apigatewayv2")
    api = client.get_api(ApiId=api_id)
    assert api["ProtocolType"] == "WEBSOCKET"
    assert api["RouteSelectionExpression"] == "$request.body.action"

    routes = client.get_routes(ApiId=api_id)["Items"]
    actual_route_keys = {route["RouteKey"] for route in routes}
    assert actual_route_keys == expected_route_keys, (
        f"Expected WebSocket route keys {expected_route_keys}, got {actual_route_keys}"
    )

    integrations = client.get_integrations(ApiId=api_id)["Items"]
    assert integrations, f"Expected integrations on WebSocket API {api_id}"
    assert {integration["IntegrationType"] for integration in integrations} == {"AWS_PROXY"}
    if expected_integration_count is not None:
        assert len(integrations) == expected_integration_count, (
            f"Expected {expected_integration_count} integrations, got {len(integrations)}"
        )


def assert_websocket_api_authorizers(api_id: str, *, expected_types: list[str]) -> None:
    """Assert a WebSocket API has authorizers with expected types."""
    client = _boto3_session().client("apigatewayv2")
    resp = client.get_authorizers(ApiId=api_id)
    actual = sorted(authorizer["AuthorizerType"] for authorizer in resp.get("Items", []))
    assert actual == sorted(expected_types), (
        f"Expected WebSocket authorizer types {sorted(expected_types)}, got {actual}"
    )


def assert_websocket_api_route_auth(api_id: str, *, route_key: str, auth_type: str) -> None:
    """Assert a WebSocket route has the expected authorization type."""
    client = _boto3_session().client("apigatewayv2")
    routes = client.get_routes(ApiId=api_id)["Items"]
    matching = [route for route in routes if route["RouteKey"] == route_key]
    assert len(matching) == 1, (
        f"Expected one WebSocket route '{route_key}', got {len(matching)}. "
        f"Available: {[route['RouteKey'] for route in routes]}"
    )
    actual = matching[0].get("AuthorizationType", "NONE")
    assert actual == auth_type, f"Expected auth type '{auth_type}' on {route_key}, got '{actual}'"


def assert_websocket_api_integrations_share_uri(
    api_id: str,
    *,
    expected_function_arn: str,
) -> None:
    """Assert all WebSocket API integrations invoke the same Lambda ARN."""
    client = _boto3_session().client("apigatewayv2")
    resp = client.get_integrations(ApiId=api_id)
    items = resp.get("Items", [])
    assert items, f"Expected integrations on WebSocket API {api_id}"
    uris = [item["IntegrationUri"] for item in items]
    assert len(set(uris)) == 1, f"Expected one shared IntegrationUri, got {uris}"
    assert expected_function_arn in uris[0], (
        f"Expected IntegrationUri to contain {expected_function_arn}, got {uris[0]}"
    )


def assert_lambda_role_policy_resources(
    role_name: str,
    *,
    expected_actions: list[str],
    expected_resources: list[str],
) -> None:
    """Assert a Lambda role's custom policy has the given actions and resources.

    Checks only Stelvio-created policies (skips AWS managed policies).
    """
    iam = _boto3_session().client("iam")
    resp = iam.list_attached_role_policies(RoleName=role_name)
    policies = resp["AttachedPolicies"]
    custom_policies = [p for p in policies if ":aws:policy/" not in p["PolicyArn"]]
    assert custom_policies, (
        f"No custom policies found on role '{role_name}'. "
        f"Attached: {[p['PolicyArn'] for p in policies]}"
    )

    all_actions: set[str] = set()
    all_resources: set[str] = set()
    for policy in custom_policies:
        policy_resp = iam.get_policy(PolicyArn=policy["PolicyArn"])
        version_id = policy_resp["Policy"]["DefaultVersionId"]
        version_resp = iam.get_policy_version(
            PolicyArn=policy["PolicyArn"],
            VersionId=version_id,
        )
        document = version_resp["PolicyVersion"]["Document"]
        if isinstance(document, str):
            document = json.loads(document)

        for statement in document.get("Statement", []):
            actions = statement.get("Action", [])
            if isinstance(actions, str):
                actions = [actions]
            all_actions.update(actions)

            resources = statement.get("Resource", [])
            if isinstance(resources, str):
                resources = [resources]
            all_resources.update(resources)

    missing_actions = set(expected_actions) - all_actions
    assert not missing_actions, (
        f"Missing IAM actions: {sorted(missing_actions)}. Actual: {sorted(all_actions)}"
    )
    missing_resources = set(expected_resources) - all_resources
    assert not missing_resources, (
        f"Missing IAM resources: {sorted(missing_resources)}. Actual: {sorted(all_resources)}"
    )
