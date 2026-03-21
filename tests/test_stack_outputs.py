from pulumi.automation import OutputValue

from stelvio.stack_outputs import (
    build_flat_outputs_json,
    build_grouped_outputs_json,
    format_grouped_outputs,
    group_stack_outputs,
)


def _state_with_components(*resources: tuple[str, str]) -> dict:
    return {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": "urn:pulumi:test::demo::pulumi:pulumi:Stack::demo-test",
                        "type": "pulumi:pulumi:Stack",
                    },
                    *(
                        {
                            "urn": urn,
                            "type": typ,
                        }
                        for urn, typ in resources
                    ),
                ]
            }
        }
    }


def _component_urn(type_name: str, name: str) -> str:
    return f"urn:pulumi:test::demo::stelvio:aws:{type_name}::{name}"


def test_group_stack_outputs_groups_components_and_user_defined() -> None:
    state = _state_with_components(
        (_component_urn("Function", "api-handler"), "stelvio:aws:Function"),
        (_component_urn("Api", "rest"), "stelvio:aws:Api"),
    )
    outputs = {
        "function_api-handler_name": OutputValue("handler-name", False),
        "function_api-handler_arn": OutputValue("handler-arn", False),
        "api_rest_invoke_url": OutputValue("https://example.com", False),
        "custom_output": OutputValue("custom", False),
    }

    grouped = group_stack_outputs(outputs, state)

    assert [group.component.display_name for group in grouped.components] == [
        "Function/api-handler",
        "Api/rest",
    ]
    assert [entry.attribute for entry in grouped.components[0].outputs] == ["arn", "name"]
    assert grouped.user_defined[0].key == "custom_output"


def test_group_stack_outputs_filters_single_component() -> None:
    state = _state_with_components(
        (_component_urn("Function", "api-handler"), "stelvio:aws:Function"),
        (_component_urn("Api", "rest"), "stelvio:aws:Api"),
    )
    outputs = {
        "function_api-handler_arn": OutputValue("handler-arn", False),
        "api_rest_invoke_url": OutputValue("https://example.com", False),
    }

    grouped = group_stack_outputs(outputs, state, component_name="rest")

    assert [group.component.display_name for group in grouped.components] == ["Api/rest"]
    assert [entry.attribute for entry in grouped.components[0].outputs] == ["invoke_url"]
    assert grouped.user_defined == ()


def test_build_flat_outputs_json_orders_by_component_then_attribute() -> None:
    state = _state_with_components(
        (_component_urn("Function", "worker"), "stelvio:aws:Function"),
        (_component_urn("Api", "rest"), "stelvio:aws:Api"),
    )
    outputs = {
        "function_worker_name": OutputValue("worker-name", False),
        "api_rest_stage_name": OutputValue("prod", False),
        "function_worker_arn": OutputValue("worker-arn", False),
        "custom_output": OutputValue("custom", False),
    }

    flat = build_flat_outputs_json(outputs, state)

    assert list(flat) == [
        "function_worker_arn",
        "function_worker_name",
        "api_rest_stage_name",
        "custom_output",
    ]


def test_grouped_json_and_text_mask_secrets() -> None:
    state = _state_with_components(
        (_component_urn("AppSync", "graphql"), "stelvio:aws:AppSync"),
    )
    outputs = {
        "appsync_graphql_api_key": OutputValue("secret-value", True),
        "appsync_graphql_url": OutputValue("https://example.com/graphql", False),
    }

    grouped = group_stack_outputs(outputs, state)

    assert build_grouped_outputs_json(grouped) == {
        "components": {
            "graphql": {
                "api_key": "[secret]",
                "url": "https://example.com/graphql",
            }
        }
    }
    assert format_grouped_outputs(grouped) == [
        "",
        "[bold]Outputs:",
        "  [bold]AppSync[/bold]  graphql",
        "    [cyan]api_key[/cyan]  [secret]",
        "    [cyan]url    [/cyan]  https://example.com/graphql",
        "",
    ]


def test_format_grouped_outputs_wraps_long_values_with_consistent_indent(monkeypatch) -> None:
    state = _state_with_components(
        (_component_urn("Function", "notifications-on-notify"), "stelvio:aws:Function"),
    )
    outputs = {
        "function_notifications-on-notify_arn": OutputValue(
            "arn:aws:lambda:us-east-1:482403859050:function:"
            "stelvio-app-michal-notifications-on-notify-a5935dd",
            False,
        ),
    }

    grouped = group_stack_outputs(outputs, state)
    monkeypatch.setattr("stelvio.stack_outputs._output_display_width", lambda: 60)

    assert format_grouped_outputs(grouped) == [
        "",
        "[bold]Outputs:",
        "  [bold]Function[/bold]  notifications-on-notify",
        "    [cyan]arn[/cyan]  arn:aws:lambda:us-east-1:482403859050:function:stel",
        "         vio-app-michal-notifications-on-notify-a5935dd",
        "",
    ]
