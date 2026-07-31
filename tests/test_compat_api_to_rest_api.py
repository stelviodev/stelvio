"""Tests for Api → RestApi compat migration planning and execution."""

from __future__ import annotations

from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from stelvio.compat.api_to_rest_api import (
    ActionKind,
    AwsClients,
    delete_domain,
    delete_log_group,
    execute_actions,
    plan_actions,
)
from tests.cli_test_helpers import import_cli_module


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "op")


def _api_state(
    *,
    component_type: str = "stelvio:aws:Api",
    with_domain: bool = True,
    with_log_group: bool = False,
    rest_api_name: str = "app-dev-my-api",
    domain_name: str = "api.example.com",
) -> dict:
    stack_urn = "urn:pulumi:dev::app::pulumi:pulumi:Stack::app-dev"
    api_urn = f"urn:pulumi:dev::app::{component_type}::my-api"
    rest_urn = "urn:pulumi:dev::app::aws:apigateway/restApi:RestApi::app-dev-my-api"
    domain_urn = (
        "urn:pulumi:dev::app::aws:apigateway/domainName:DomainName::app-dev-my-api-custom-domain"
    )
    mapping_urn = (
        "urn:pulumi:dev::app::aws:apigateway/basePathMapping:BasePathMapping::"
        "app-dev-my-api-custom-domain-base-path-mapping"
    )
    dns_urn = "urn:pulumi:dev::app::aws:route53/record:Record::app-dev-my-api-custom-domain-record"
    log_urn = "urn:pulumi:dev::app::aws:cloudwatch/logGroup:LogGroup::app-dev-my-api-logs"

    resources: list[dict] = [
        {"urn": stack_urn, "type": "pulumi:pulumi:Stack"},
        {"urn": api_urn, "type": component_type, "parent": stack_urn},
        {
            "urn": rest_urn,
            "type": "aws:apigateway/restApi:RestApi",
            "parent": api_urn,
            "outputs": {"name": rest_api_name},
        },
    ]
    if with_domain:
        resources.extend(
            [
                {
                    "urn": domain_urn,
                    "type": "aws:apigateway/domainName:DomainName",
                    "parent": api_urn,
                    "outputs": {"domainName": domain_name},
                },
                {
                    "urn": mapping_urn,
                    "type": "aws:apigateway/basePathMapping:BasePathMapping",
                    "parent": api_urn,
                },
                {
                    "urn": dns_urn,
                    "type": "aws:route53/record:Record",
                    "parent": api_urn,
                },
            ]
        )
    if with_log_group:
        resources.append(
            {
                "urn": log_urn,
                "type": "aws:cloudwatch/logGroup:LogGroup",
                "parent": api_urn,
                "outputs": {"name": f"/aws/apigateway/{rest_api_name}"},
            }
        )
    return {"checkpoint": {"latest": {"resources": resources}}}


def test_plan_actions_with_domain_and_unmanaged_log_group() -> None:
    state = _api_state()
    existing = {"/aws/apigateway/app-dev-my-api"}

    actions = plan_actions(state, log_group_exists=existing.__contains__)

    kinds = [a.kind for a in actions]
    assert ActionKind.DELETE_DOMAIN in kinds
    assert kinds.count(ActionKind.STATE_RM) == 3
    assert ActionKind.DELETE_LOG_GROUP in kinds
    log_action = next(a for a in actions if a.kind == ActionKind.DELETE_LOG_GROUP)
    assert log_action.log_group_name == "/aws/apigateway/app-dev-my-api"


def test_plan_actions_skips_log_group_when_already_managed() -> None:
    state = _api_state(with_domain=False, with_log_group=True)
    existing = {"/aws/apigateway/app-dev-my-api"}

    actions = plan_actions(state, log_group_exists=existing.__contains__)

    assert actions == []


def test_plan_actions_empty_without_api_components() -> None:
    state = {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": "urn:pulumi:dev::app::pulumi:pulumi:Stack::app-dev",
                        "type": "pulumi:pulumi:Stack",
                    }
                ]
            }
        }
    }
    actions = plan_actions(state, log_group_exists=lambda _: True)
    assert actions == []


def test_plan_actions_supports_rest_api_component_type() -> None:
    state = _api_state(
        component_type="stelvio:aws:RestApi",
        with_domain=True,
        with_log_group=False,
    )
    actions = plan_actions(state, log_group_exists=lambda _: False)
    assert any(a.kind == ActionKind.DELETE_DOMAIN for a in actions)
    assert not any(a.kind == ActionKind.DELETE_LOG_GROUP for a in actions)


def test_execute_non_interactive_runs_all() -> None:
    state = _api_state()
    actions = plan_actions(state, log_group_exists=lambda _: True)
    apigateway = Mock()
    logs = Mock()

    results = execute_actions(
        actions,
        state,
        AwsClients(apigateway=apigateway, logs=logs),
        interactive=False,
    )

    assert all(r.status == "done" for r in results)
    apigateway.delete_domain_name.assert_called_once_with(domainName="api.example.com")
    logs.delete_log_group.assert_called_once_with(logGroupName="/aws/apigateway/app-dev-my-api")
    remaining_types = {r["type"] for r in state["checkpoint"]["latest"]["resources"]}
    assert "aws:apigateway/domainName:DomainName" not in remaining_types
    assert "aws:apigateway/basePathMapping:BasePathMapping" not in remaining_types
    assert "aws:route53/record:Record" not in remaining_types


def test_execute_interactive_skip() -> None:
    state = _api_state(with_domain=False)
    actions = plan_actions(state, log_group_exists=lambda _: True)
    assert len(actions) == 1

    results = execute_actions(
        actions,
        state,
        AwsClients(apigateway=Mock(), logs=Mock()),
        interactive=True,
        confirm=lambda _msg: False,
    )

    assert results[0].status == "skipped"


def test_execute_interactive_confirm_runs() -> None:
    state = _api_state(with_domain=False)
    actions = plan_actions(state, log_group_exists=lambda _: True)
    logs = Mock()

    results = execute_actions(
        actions,
        state,
        AwsClients(apigateway=Mock(), logs=logs),
        interactive=True,
        confirm=lambda _msg: True,
    )

    assert results[0].status == "done"
    logs.delete_log_group.assert_called_once()


def test_delete_domain_treats_not_found_as_success() -> None:
    apigateway = Mock()
    apigateway.delete_domain_name.side_effect = _client_error("NotFoundException")
    detail = delete_domain(apigateway, "api.example.com")
    assert "already absent" in detail


def test_delete_log_group_treats_not_found_as_success() -> None:
    logs = Mock()
    logs.delete_log_group.side_effect = _client_error("ResourceNotFoundException")
    detail = delete_log_group(logs, "/aws/apigateway/x")
    assert "already absent" in detail


def test_compat_api_to_rest_api_cli_wires_flags() -> None:
    cli_module = import_cli_module()

    with (
        patch.object(cli_module, "ensure_pulumi"),
        patch.object(cli_module, "determine_env", return_value="dev"),
        patch.object(cli_module, "run_compat_api_to_rest_api", return_value=0) as run_mock,
    ):
        result = cli_module.compat_api_to_rest_api.main(["staging", "-i"], standalone_mode=False)

    assert result is None
    run_mock.assert_called_once_with("dev", interactive=True)
