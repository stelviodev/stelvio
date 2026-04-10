from stelvio.state_ops import build_state_tree, build_state_tree_json, repair_state


def _state_with_resources(resources: list[dict]) -> dict:
    return {"checkpoint": {"latest": {"resources": resources}}}


def _urn(type_token: str, name: str) -> str:
    return f"urn:pulumi:dev::myapp::{type_token}::{name}"


def test_build_state_tree_groups_stack_components_and_providers() -> None:
    stack_urn = _urn("pulumi:pulumi:Stack", "myapp-dev")
    api_component_urn = _urn("stelvio:aws:Function", "api")
    lambda_urn = _urn("aws:lambda/function:Function", "myapp-dev-api")
    role_urn = _urn("aws:iam/role:Role", "myapp-dev-api-r")
    provider_urn = _urn("pulumi:providers:aws", "default_6_78_0")
    state = _state_with_resources(
        [
            {"urn": stack_urn, "type": "pulumi:pulumi:Stack"},
            {
                "urn": api_component_urn,
                "type": "stelvio:aws:Function",
                "parent": stack_urn,
            },
            {
                "urn": lambda_urn,
                "type": "aws:lambda/function:Function",
                "parent": api_component_urn,
                "dependencies": [role_urn],
            },
            {
                "urn": role_urn,
                "type": "aws:iam/role:Role",
                "parent": api_component_urn,
            },
            {"urn": provider_urn, "type": "pulumi:providers:aws"},
        ]
    )

    grouped = build_state_tree(state)

    assert grouped.stack is not None
    assert grouped.stack.name == "myapp-dev"
    assert [node.resource.name for node in grouped.components] == ["api"]
    assert [child.resource.name for child in grouped.components[0].children] == [
        "myapp-dev-api",
        "myapp-dev-api-r",
    ]
    assert [node.resource.name for node in grouped.providers] == ["default_6_78_0"]
    assert grouped.other_roots == ()


def test_build_state_tree_json_includes_stack_components_and_providers() -> None:
    stack_urn = _urn("pulumi:pulumi:Stack", "myapp-dev")
    topic_urn = _urn("stelvio:aws:Topic", "notifications")
    subscription_urn = _urn("stelvio:aws:TopicSubscription", "on-notify")
    function_urn = _urn("stelvio:aws:Function", "notifications-on-notify")
    lambda_urn = _urn("aws:lambda/function:Function", "myapp-dev-notifications-on-notify")
    provider_urn = _urn("pulumi:providers:aws", "stelvio-aws")
    state = _state_with_resources(
        [
            {"urn": stack_urn, "type": "pulumi:pulumi:Stack"},
            {
                "urn": topic_urn,
                "type": "stelvio:aws:Topic",
                "parent": stack_urn,
            },
            {
                "urn": subscription_urn,
                "type": "stelvio:aws:TopicSubscription",
                "parent": topic_urn,
            },
            {
                "urn": function_urn,
                "type": "stelvio:aws:Function",
                "parent": subscription_urn,
            },
            {
                "urn": lambda_urn,
                "type": "aws:lambda/function:Function",
                "parent": function_urn,
            },
            {"urn": provider_urn, "type": "pulumi:providers:aws"},
        ]
    )

    grouped = build_state_tree(state)

    assert build_state_tree_json(grouped) == {
        "stack": {
            "name": "myapp-dev",
            "urn": stack_urn,
            "type": "pulumi:pulumi:Stack",
            "parent": None,
            "dependencies": [],
        },
        "components": [
            {
                "name": "notifications",
                "urn": topic_urn,
                "type": "stelvio:aws:Topic",
                "parent": stack_urn,
                "dependencies": [],
                "component_type": "Topic",
                "children": [
                    {
                        "name": "on-notify",
                        "urn": subscription_urn,
                        "type": "stelvio:aws:TopicSubscription",
                        "parent": topic_urn,
                        "dependencies": [],
                        "component_type": "TopicSubscription",
                        "children": [
                            {
                                "name": "notifications-on-notify",
                                "urn": function_urn,
                                "type": "stelvio:aws:Function",
                                "parent": subscription_urn,
                                "dependencies": [],
                                "component_type": "Function",
                                "children": [
                                    {
                                        "name": "myapp-dev-notifications-on-notify",
                                        "urn": lambda_urn,
                                        "type": "aws:lambda/function:Function",
                                        "parent": function_urn,
                                        "dependencies": [],
                                        "children": [],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
        "providers": [
            {
                "name": "stelvio-aws",
                "urn": provider_urn,
                "type": "pulumi:providers:aws",
                "parent": None,
                "dependencies": [],
                "children": [],
            }
        ],
    }


def test_build_state_tree_preserves_provider_order_from_state() -> None:
    stack_urn = _urn("pulumi:pulumi:Stack", "myapp-dev")
    first_provider_urn = _urn("pulumi:providers:aws", "stelvio-aws")
    second_provider_urn = _urn("pulumi:providers:aws", "default_6_78_0")
    state = _state_with_resources(
        [
            {"urn": stack_urn, "type": "pulumi:pulumi:Stack"},
            {"urn": first_provider_urn, "type": "pulumi:providers:aws"},
            {"urn": second_provider_urn, "type": "pulumi:providers:aws"},
        ]
    )

    grouped = build_state_tree(state)

    assert [node.resource.name for node in grouped.providers] == ["stelvio-aws", "default_6_78_0"]


def test_repair_state_clears_pending_operations() -> None:
    pending_urn_1 = "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-old-role"
    pending_urn_2 = "urn:pulumi:dev::myapp::aws:lambda/function:Function::myapp-dev-old-fn"
    state = {
        "checkpoint": {
            "latest": {
                "resources": [
                    {
                        "urn": "urn:pulumi:dev::myapp::aws:s3/bucketV2:BucketV2::uploads",
                        "type": "aws:s3/bucketV2:BucketV2",
                    }
                ],
                "pending_operations": [
                    {"type": "creating", "resource": {"urn": pending_urn_1}},
                    {"type": "deleting", "resource": pending_urn_2},
                ],
            }
        }
    }

    mutations = repair_state(state)

    assert state["checkpoint"]["latest"]["pending_operations"] == []
    pending_mutations = [m for m in mutations if m.action == "remove_pending_operation"]
    assert len(pending_mutations) == 2
    assert pending_mutations[0].target_urn == pending_urn_1
    assert pending_mutations[1].target_urn == pending_urn_2
    assert "creating" in pending_mutations[0].detail
    assert "old-role" in pending_mutations[0].detail
    assert "deleting" in pending_mutations[1].detail
    assert "old-fn" in pending_mutations[1].detail


def test_repair_state_pending_operation_without_resource_uses_unknown_target() -> None:
    state = {
        "checkpoint": {
            "latest": {
                "resources": [],
                "pending_operations": [{"type": "updating", "resource": {}}],
            }
        }
    }

    mutations = repair_state(state)

    assert state["checkpoint"]["latest"]["pending_operations"] == []
    assert len(mutations) == 1
    assert mutations[0].action == "remove_pending_operation"
    assert mutations[0].target_urn == ""
    assert "<unknown-resource>" in mutations[0].detail
