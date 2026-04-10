from pulumi.automation import OutputValue

from stelvio.stack_outputs import (
    build_outputs_json,
    format_outputs,
    get_deployed_components,
    group_outputs,
)


def _component_urn(type_name: str, name: str) -> str:
    return f"urn:pulumi:test::demo::stelvio:aws:{type_name}::{name}"


def _state_with_components(
    *resources: tuple[str, str] | tuple[str, str, str | None] | tuple[str, str, str | None, dict],
) -> dict:
    resource_entries: list[dict] = []
    for resource in resources:
        urn = resource[0]
        typ = resource[1]
        parent = resource[2] if len(resource) >= 3 else None
        outputs = resource[3] if len(resource) >= 4 else None
        entry: dict = {"urn": urn, "type": typ}
        if parent is not None:
            entry["parent"] = parent
        if outputs is not None:
            entry["outputs"] = outputs
        resource_entries.append(entry)

    return {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": "urn:pulumi:test::demo::pulumi:pulumi:Stack::demo-test",
                        "type": "pulumi:pulumi:Stack",
                    },
                    *resource_entries,
                ]
            }
        }
    }


def test_get_deployed_components_reads_outputs_from_state() -> None:
    state = _state_with_components(
        (
            _component_urn("Api", "rest"),
            "stelvio:aws:Api",
            None,
            {"url": "https://example.com/api"},
        ),
    )
    components = get_deployed_components(state)

    assert len(components) == 1
    assert components[0].name == "rest"
    assert components[0].outputs == {"url": "https://example.com/api"}


def test_get_deployed_components_filters_underscore_prefixed_outputs() -> None:
    state = _state_with_components(
        (
            _component_urn("Api", "rest"),
            "stelvio:aws:Api",
            None,
            {"url": "https://example.com", "_dev": {"command": "npm start"}},
        ),
    )
    components = get_deployed_components(state)

    assert components[0].outputs == {"url": "https://example.com"}


def test_get_deployed_components_handles_no_outputs() -> None:
    state = _state_with_components(
        (_component_urn("Function", "worker"), "stelvio:aws:Function"),
    )
    components = get_deployed_components(state)

    assert len(components) == 1
    assert components[0].outputs == {}


def test_group_outputs_with_component_values_and_user_exports() -> None:
    state = _state_with_components(
        (
            _component_urn("Api", "rest"),
            "stelvio:aws:Api",
            None,
            {"url": "https://api.example.com"},
        ),
    )
    user_outputs = {
        "api_url": OutputValue("https://api.example.com", False),
        "stage": OutputValue("production", False),
    }

    grouped = group_outputs(state, user_outputs)

    assert len(grouped.components) == 1
    assert grouped.components[0].component.name == "rest"
    assert grouped.components[0].outputs[0].key == "url"
    assert grouped.components[0].outputs[0].value == "https://api.example.com"
    assert len(grouped.user_defined) == 2
    assert grouped.user_defined[0].key == "api_url"
    assert grouped.user_defined[1].key == "stage"


def test_group_outputs_skips_components_without_display_outputs() -> None:
    state = _state_with_components(
        (
            _component_urn("Api", "rest"),
            "stelvio:aws:Api",
            None,
            {"url": "https://api.example.com"},
        ),
        (_component_urn("Function", "worker"), "stelvio:aws:Function"),
        (_component_urn("DynamoTable", "users"), "stelvio:aws:DynamoTable"),
    )

    grouped = group_outputs(state)

    assert len(grouped.components) == 1
    assert grouped.components[0].component.type_name == "Api"


def test_group_outputs_renders_nested_tree() -> None:
    parent_urn = _component_urn("TopicSubscription", "on-event")
    child_urn = _component_urn("Function", "handler")
    # Child has no display output, parent has no display output,
    # but if child had an output, the tree should render
    state = _state_with_components(
        (parent_urn, "stelvio:aws:TopicSubscription"),
        (child_urn, "stelvio:aws:Function", parent_urn),
    )

    grouped = group_outputs(state)
    assert len(grouped.components) == 0  # No display outputs anywhere


def test_format_outputs_displays_component_tree_with_urls() -> None:
    state = _state_with_components(
        (
            _component_urn("Api", "rest"),
            "stelvio:aws:Api",
            None,
            {"url": "https://api.example.com"},
        ),
        (
            _component_urn("Router", "cdn"),
            "stelvio:aws:Router",
            None,
            {"url": "https://cdn.example.com"},
        ),
    )

    grouped = group_outputs(state)
    lines = format_outputs(grouped)

    assert lines == [
        "",
        "[bold]Outputs:",
        "  [bold]Api[/bold] rest",
        "    [cyan]url[/cyan]  https://api.example.com",
        "  [bold]Router[/bold] cdn",
        "    [cyan]url[/cyan]  https://cdn.example.com",
    ]


def test_format_outputs_with_user_defined_section() -> None:
    state = _state_with_components(
        (
            _component_urn("Api", "rest"),
            "stelvio:aws:Api",
            None,
            {"url": "https://api.example.com"},
        ),
    )
    user_outputs = {"api_url": OutputValue("https://api.example.com", False)}

    grouped = group_outputs(state, user_outputs)
    lines = format_outputs(grouped)

    assert "  [bold]User defined[/bold]" in lines
    assert "    [cyan]api_url[/cyan]  https://api.example.com" in lines


def test_format_outputs_wraps_long_values(monkeypatch) -> None:
    state = _state_with_components(
        (
            _component_urn("Api", "my-api"),
            "stelvio:aws:Api",
            None,
            {"url": "https://abc123def456.execute-api.us-east-1.amazonaws.com/prod"},
        ),
    )

    grouped = group_outputs(state)
    monkeypatch.setattr("stelvio.stack_outputs._output_display_width", lambda: 50)
    lines = format_outputs(grouped)

    # Should wrap the long URL
    assert len(lines) > 4  # More than header + component + single output line + footer


def test_format_outputs_empty_when_no_outputs() -> None:
    state = _state_with_components(
        (_component_urn("Function", "worker"), "stelvio:aws:Function"),
    )

    grouped = group_outputs(state)
    assert format_outputs(grouped) == []


def test_format_outputs_user_defined_only() -> None:
    grouped = group_outputs(None, {"key": OutputValue("value", False)})
    lines = format_outputs(grouped)

    assert "  [bold]User defined[/bold]" in lines
    assert "    [cyan]key[/cyan]  value" in lines


def test_build_outputs_json_with_components_and_user_defined() -> None:
    state = _state_with_components(
        (
            _component_urn("Api", "rest"),
            "stelvio:aws:Api",
            None,
            {"url": "https://api.example.com"},
        ),
    )
    user_outputs = {"stage": OutputValue("prod", False)}

    grouped = group_outputs(state, user_outputs)
    data = build_outputs_json(grouped)

    assert data == {
        "components": [
            {
                "type": "Api",
                "name": "rest",
                "outputs": {"url": "https://api.example.com"},
            }
        ],
        "user_defined": {"stage": "prod"},
    }


def test_build_outputs_json_empty() -> None:
    grouped = group_outputs(None)
    assert build_outputs_json(grouped) == {}


def test_build_outputs_json_nested_components() -> None:
    parent_urn = _component_urn("S3StaticWebsite", "site")
    child_urn = _component_urn("CloudFrontDistribution", "site-cf")
    state = _state_with_components(
        (
            parent_urn,
            "stelvio:aws:S3StaticWebsite",
            None,
            {"url": "https://site.example.com"},
        ),
        (
            child_urn,
            "stelvio:aws:CloudFrontDistribution",
            parent_urn,
            {"url": "https://d123.cloudfront.net"},
        ),
    )

    grouped = group_outputs(state)
    data = build_outputs_json(grouped)

    assert data == {
        "components": [
            {
                "type": "S3StaticWebsite",
                "name": "site",
                "outputs": {"url": "https://site.example.com"},
                "components": [
                    {
                        "type": "CloudFrontDistribution",
                        "name": "site-cf",
                        "outputs": {"url": "https://d123.cloudfront.net"},
                    }
                ],
            }
        ]
    }


def test_user_defined_masks_secrets() -> None:
    user_outputs = {
        "api_key": OutputValue("secret-value", True),
        "url": OutputValue("https://example.com", False),
    }

    grouped = group_outputs(None, user_outputs)

    assert grouped.user_defined[0].key == "api_key"
    assert grouped.user_defined[0].value == "secret-value"  # Raw value preserved
    assert grouped.user_defined[0].display_value == "[secret]"  # Display masks it
    assert grouped.user_defined[0].secret is True
    assert grouped.user_defined[1].value == "https://example.com"
    assert grouped.user_defined[1].display_value == "https://example.com"


def test_json_preserves_non_string_types() -> None:
    user_outputs = {
        "count": OutputValue(3, False),
        "config": OutputValue({"a": 1, "b": 2}, False),
        "tags": OutputValue(["web", "api"], False),
        "hidden_val": OutputValue("s3cr3t", True),
    }

    grouped = group_outputs(None, user_outputs)
    data = build_outputs_json(grouped)

    assert data["user_defined"]["count"] == 3
    assert data["user_defined"]["config"] == {"a": 1, "b": 2}
    assert data["user_defined"]["tags"] == ["web", "api"]
    assert data["user_defined"]["hidden_val"] == "[secret]"


def test_human_display_stringifies_non_string_values() -> None:
    user_outputs = {"count": OutputValue(42, False)}

    grouped = group_outputs(None, user_outputs)
    lines = format_outputs(grouped)

    assert any("42" in line for line in lines)


def test_get_deployed_components_returns_empty_for_none_state() -> None:
    assert get_deployed_components(None) == []
