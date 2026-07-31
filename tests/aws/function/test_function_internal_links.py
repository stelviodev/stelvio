"""Behavioral tests for Function component-managed internal links.

Composed components register links via the private
``_register_internal_link`` / ``_register_internal_link_initializer`` API.
Assertions target observable outcomes: IAM policy, STLV_ env vars,
stlv_resources codegen, config immutability, and lifecycle errors.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pulumi
from pytest import raises

from stelvio.aws.function import Function
from stelvio.aws.permission import AwsPermission
from stelvio.link import Link

from ...conftest import TP

if TYPE_CHECKING:
    from pathlib import Path

INTERNAL_LINK = Link(
    "managed",
    properties={"endpoint": "https://api.example.com", "stage": "prod"},
    permissions=[
        AwsPermission(
            actions=["execute-api:ManageConnections"],
            resources=["arn:aws:execute-api:us-east-1:123456789012:api/*/stage/@connections/*"],
        )
    ],
)

INTERNAL_LINK_OTHER = Link(
    "managed",
    properties={"endpoint": "https://other.example.com"},
    permissions=[
        AwsPermission(
            actions=["execute-api:ManageConnections"],
            resources=["arn:aws:execute-api:us-east-1:123456789012:other/*/stage/@connections/*"],
        )
    ],
)

SECOND_INTERNAL_LINK = Link(
    "callback",
    properties={"url": "https://callback.example.com"},
    permissions=[
        AwsPermission(
            actions=["lambda:InvokeFunction"],
            resources=["arn:aws:lambda:us-east-1:123456789012:function:callback"],
        )
    ],
)

USER_LINK = Link(
    "user-table",
    properties={"table_name": "orders"},
    permissions=[
        AwsPermission(
            actions=["dynamodb:GetItem"],
            resources=["arn:aws:dynamodb:us-east-1:123456789012:table/orders"],
        )
    ],
)


def _assert_policy_has_actions(pulumi_mocks, function_name: str, *expected_actions: str) -> None:
    policies = pulumi_mocks.created_policies(f"{TP + function_name}-p")
    assert len(policies) == 1
    statements = json.loads(policies[0].inputs["policy"])
    actions = {action for statement in statements for action in statement["actions"]}
    for action in expected_actions:
        assert action in actions


def _assert_env_vars(pulumi_mocks, function_name: str, expected: dict[str, str]) -> None:
    functions = pulumi_mocks.created_functions(TP + function_name)
    assert len(functions) == 1
    env_vars = functions[0].inputs["environment"]["variables"]
    for key, value in expected.items():
        assert env_vars[key] == value


def _assert_codegen_contains(project_cwd: Path, *snippets: str) -> None:
    ide_file = project_cwd / "functions" / "stlv_resources.py"
    assert ide_file.exists()
    content = ide_file.read_text()
    for snippet in snippets:
        assert snippet in content


@pulumi.runtime.test
def test_internal_link_appears_in_permissions_env_and_codegen(pulumi_mocks, project_cwd):
    function = Function("worker", handler="functions/simple.handler")
    function._register_internal_link(INTERNAL_LINK)

    def check(_):
        _assert_policy_has_actions(pulumi_mocks, "worker", "execute-api:ManageConnections")
        _assert_env_vars(
            pulumi_mocks,
            "worker",
            {
                "STLV_MANAGED_ENDPOINT": "https://api.example.com",
                "STLV_MANAGED_STAGE": "prod",
            },
        )
        _assert_codegen_contains(
            project_cwd,
            "class ManagedResource:",
            'return os.environ["STLV_MANAGED_ENDPOINT"]',
            'return os.environ["STLV_MANAGED_STAGE"]',
            "managed: Final[ManagedResource]",
        )

    return function.invoke_arn.apply(check)


@pulumi.runtime.test
def test_registering_same_internal_link_twice_is_idempotent(pulumi_mocks, project_cwd):
    function = Function("worker", handler="functions/simple.handler")
    function._register_internal_link(INTERNAL_LINK)
    function._register_internal_link(INTERNAL_LINK)

    def check(_):
        assert len(function._internal_links) == 1
        policies = pulumi_mocks.created_policies(f"{TP}worker-p")
        assert len(policies) == 1
        statements = json.loads(policies[0].inputs["policy"])
        assert len(statements) == 1
        assert statements[0]["actions"] == ["execute-api:ManageConnections"]

    return function.invoke_arn.apply(check)


def test_conflicting_internal_link_name_raises_value_error(project_cwd):
    function = Function("worker", handler="functions/simple.handler")
    function._register_internal_link(INTERNAL_LINK)

    with raises(
        ValueError,
        match=(
            r"Function 'worker' already has a different component-managed link "
            r"named 'managed'\."
        ),
    ):
        function._register_internal_link(INTERNAL_LINK_OTHER)


def test_user_configured_link_name_conflict_raises_value_error(project_cwd):
    function = Function(
        "worker",
        handler="functions/simple.handler",
        links=[Link("managed", properties={"endpoint": "user"}, permissions=[])],
    )

    with raises(
        ValueError,
        match=(
            r"Function 'worker' already has a user-configured link named "
            r"'managed', which conflicts with a component-managed link\."
        ),
    ):
        function._register_internal_link(INTERNAL_LINK)


def test_user_configured_linkable_name_conflict_raises_value_error(project_cwd):
    shared = Function("managed", handler="functions/simple.handler")
    worker = Function(
        "worker",
        handler="functions/simple.handler",
        links=[shared],
    )

    with raises(
        ValueError,
        match=(
            r"Function 'worker' already has a user-configured link named "
            r"'managed', which conflicts with a component-managed link\."
        ),
    ):
        worker._register_internal_link(INTERNAL_LINK)


@pulumi.runtime.test
def test_registration_after_resources_raises_runtime_error(pulumi_mocks, project_cwd):
    function = Function("worker", handler="functions/simple.handler")

    def check(_):
        with raises(
            RuntimeError,
            match=(
                r"Cannot register internal link 'managed' on Function 'worker' after "
                r"resources have been created\. Register composed-component links before "
                r"accessing the Function's \.resources property\."
            ),
        ):
            function._register_internal_link(INTERNAL_LINK)

    return function.invoke_arn.apply(check)


@pulumi.runtime.test
def test_initializer_registers_link_before_effective_links_consumed(pulumi_mocks, project_cwd):
    function = Function("worker", handler="functions/simple.handler")

    def initializer() -> None:
        function._register_internal_link(INTERNAL_LINK)

    function._register_internal_link_initializer("parent-api", initializer)

    def check(_):
        _assert_policy_has_actions(pulumi_mocks, "worker", "execute-api:ManageConnections")
        _assert_env_vars(
            pulumi_mocks,
            "worker",
            {
                "STLV_MANAGED_ENDPOINT": "https://api.example.com",
                "STLV_MANAGED_STAGE": "prod",
            },
        )

    return function.invoke_arn.apply(check)


@pulumi.runtime.test
def test_initializer_registration_after_resources_raises_runtime_error(pulumi_mocks, project_cwd):
    function = Function("worker", handler="functions/simple.handler")

    def check(_):
        with raises(
            RuntimeError,
            match=(
                r"Cannot register an internal link initializer on Function 'worker' after "
                r"resources have been created\. Register composed-component links before "
                r"accessing the Function's \.resources property\."
            ),
        ):
            function._register_internal_link_initializer("parent", lambda: None)

    return function.invoke_arn.apply(check)


@pulumi.runtime.test
def test_initializer_key_replace_uses_latest_initializer(pulumi_mocks, project_cwd):
    function = Function("worker", handler="functions/simple.handler")
    first_ran = {"value": False}

    def first_initializer() -> None:
        first_ran["value"] = True
        function._register_internal_link(INTERNAL_LINK)

    def second_initializer() -> None:
        function._register_internal_link(SECOND_INTERNAL_LINK)

    function._register_internal_link_initializer("parent", first_initializer)
    function._register_internal_link_initializer("parent", second_initializer)

    def check(_):
        assert first_ran["value"] is False
        _assert_env_vars(
            pulumi_mocks,
            "worker",
            {"STLV_CALLBACK_URL": "https://callback.example.com"},
        )
        functions = pulumi_mocks.created_functions(f"{TP}worker")
        env_vars = functions[0].inputs["environment"]["variables"]
        assert "STLV_MANAGED_ENDPOINT" not in env_vars
        _assert_policy_has_actions(pulumi_mocks, "worker", "lambda:InvokeFunction")

    return function.invoke_arn.apply(check)


@pulumi.runtime.test
def test_two_parents_register_distinct_links_on_shared_function(pulumi_mocks, project_cwd):
    function = Function("shared", handler="functions/simple.handler")
    function._register_internal_link(INTERNAL_LINK)
    function._register_internal_link(SECOND_INTERNAL_LINK)

    def check(_):
        _assert_env_vars(
            pulumi_mocks,
            "shared",
            {
                "STLV_MANAGED_ENDPOINT": "https://api.example.com",
                "STLV_MANAGED_STAGE": "prod",
                "STLV_CALLBACK_URL": "https://callback.example.com",
            },
        )
        _assert_policy_has_actions(
            pulumi_mocks,
            "shared",
            "execute-api:ManageConnections",
            "lambda:InvokeFunction",
        )
        _assert_codegen_contains(
            project_cwd,
            "managed: Final[ManagedResource]",
            "callback: Final[CallbackResource]",
        )

    return function.invoke_arn.apply(check)


def test_internal_link_does_not_mutate_function_config(project_cwd):
    function = Function(
        "worker",
        handler="functions/simple.handler",
        links=[USER_LINK],
    )
    configured_links = list(function.config.links)

    function._register_internal_link(INTERNAL_LINK)

    assert function.config.links == configured_links
    assert function.config.links == [USER_LINK]
    assert "managed" not in [link.name for link in function.config.links]
    assert function._effective_links == [USER_LINK, INTERNAL_LINK]


@pulumi.runtime.test
def test_user_only_links_unchanged_when_no_internal_links(pulumi_mocks, project_cwd):
    function = Function(
        "worker",
        handler="functions/simple.handler",
        links=[USER_LINK],
    )

    def check(_):
        _assert_env_vars(
            pulumi_mocks,
            "worker",
            {"STLV_USER_TABLE_TABLE_NAME": "orders"},
        )
        _assert_policy_has_actions(pulumi_mocks, "worker", "dynamodb:GetItem")
        assert function._internal_links == {}

    return function.invoke_arn.apply(check)


@pulumi.runtime.test
def test_user_and_internal_links_combine_in_effective_set(pulumi_mocks, project_cwd):
    function = Function(
        "worker",
        handler="functions/simple.handler",
        links=[USER_LINK],
    )
    function._register_internal_link(INTERNAL_LINK)

    def check(_):
        _assert_env_vars(
            pulumi_mocks,
            "worker",
            {
                "STLV_USER_TABLE_TABLE_NAME": "orders",
                "STLV_MANAGED_ENDPOINT": "https://api.example.com",
                "STLV_MANAGED_STAGE": "prod",
            },
        )
        _assert_policy_has_actions(
            pulumi_mocks,
            "worker",
            "dynamodb:GetItem",
            "execute-api:ManageConnections",
        )

    return function.invoke_arn.apply(check)


def test_bridge_environment_includes_internal_link_env_vars(project_cwd):
    function = Function("worker", handler="functions/simple.handler")
    function._register_internal_link(INTERNAL_LINK)

    loop = asyncio.new_event_loop()
    try:
        env = loop.run_until_complete(function._get_environment_for_bridge_event())
    finally:
        loop.close()

    assert env["STLV_MANAGED_ENDPOINT"] == "https://api.example.com"
    assert env["STLV_MANAGED_STAGE"] == "prod"


def test_reregistering_rebuilt_output_link_is_idempotent(project_cwd):
    """Parents rebuild Links with fresh Output wrappers; identity must not conflict."""
    function = Function("worker", handler="functions/simple.handler")

    first = Link(
        "managed",
        properties={"endpoint": pulumi.Output.from_input("https://api.example.com")},
        permissions=[
            AwsPermission(
                actions=["execute-api:ManageConnections"],
                resources=[
                    pulumi.Output.from_input(
                        "arn:aws:execute-api:us-east-1:123456789012:api/*/stage/@connections/*"
                    )
                ],
            )
        ],
    )
    rebuilt = Link(
        "managed",
        properties={"endpoint": pulumi.Output.from_input("https://api.example.com")},
        permissions=[
            AwsPermission(
                actions=["execute-api:ManageConnections"],
                resources=[
                    pulumi.Output.from_input(
                        "arn:aws:execute-api:us-east-1:123456789012:api/*/stage/@connections/*"
                    )
                ],
            )
        ],
    )
    assert first != rebuilt  # dataclass == uses Output identity

    function._register_internal_link(first)
    function._register_internal_link(rebuilt)

    assert len(function._internal_links) == 1
    assert function._internal_links["managed"] is first


@pulumi.runtime.test
def test_output_valued_internal_link_flows_to_env_and_policy(pulumi_mocks, project_cwd):
    function = Function("worker", handler="functions/simple.handler")
    endpoint = pulumi.Output.from_input("https://api.example.com")
    resource_arn = pulumi.Output.from_input(
        "arn:aws:execute-api:us-east-1:123456789012:api/*/stage/@connections/*"
    )
    function._register_internal_link(
        Link(
            "managed",
            properties={"endpoint": endpoint, "stage": "prod"},
            permissions=[
                AwsPermission(
                    actions=["execute-api:ManageConnections"],
                    resources=[resource_arn],
                )
            ],
        )
    )

    def check(_):
        _assert_policy_has_actions(pulumi_mocks, "worker", "execute-api:ManageConnections")
        functions = pulumi_mocks.created_functions(f"{TP}worker")
        env_vars = functions[0].inputs["environment"]["variables"]
        assert env_vars["STLV_MANAGED_ENDPOINT"] == "https://api.example.com"
        assert env_vars["STLV_MANAGED_STAGE"] == "prod"
        _assert_codegen_contains(
            project_cwd,
            "managed: Final[ManagedResource]",
            'return os.environ["STLV_MANAGED_ENDPOINT"]',
        )

    return function.invoke_arn.apply(check)


@pulumi.runtime.test
def test_initializer_accessing_resources_raises_reentrancy_error(pulumi_mocks, project_cwd):
    function = Function("worker", handler="functions/simple.handler")

    def initializer() -> None:
        _ = function.resources

    function._register_internal_link_initializer("parent", initializer)

    with raises(
        RuntimeError,
        match=(
            r"Function 'worker' is already materializing\. Do not access "
            r"\.resources, invoke_arn, or other resource-backed properties from an "
            r"internal link initializer\."
        ),
    ):
        _ = function.resources


def test_register_after_links_locked_raises(project_cwd):
    """After the initializer phase freezes links, registration must not silently no-op."""
    function = Function("worker", handler="functions/simple.handler")
    function._internal_links_locked = True

    with raises(
        RuntimeError,
        match=(
            r"Cannot register internal link 'managed' on Function 'worker' after "
            r"resources have been created"
        ),
    ):
        function._register_internal_link(INTERNAL_LINK)


def test_initializer_registration_after_links_locked_raises(project_cwd):
    function = Function("worker", handler="functions/simple.handler")
    function._internal_links_locked = True

    with raises(
        RuntimeError,
        match=(
            r"Cannot register an internal link initializer on Function 'worker' after "
            r"resources have been created"
        ),
    ):
        function._register_internal_link_initializer("late", lambda: None)


@pulumi.runtime.test
def test_initializer_registered_by_initializer_runs(pulumi_mocks, project_cwd):
    function = Function("worker", handler="functions/simple.handler")

    def child_initializer() -> None:
        function._register_internal_link(SECOND_INTERNAL_LINK)

    def parent_initializer() -> None:
        function._register_internal_link(INTERNAL_LINK)
        function._register_internal_link_initializer("child", child_initializer)

    function._register_internal_link_initializer("parent", parent_initializer)

    def check(_):
        _assert_env_vars(
            pulumi_mocks,
            "worker",
            {
                "STLV_MANAGED_ENDPOINT": "https://api.example.com",
                "STLV_CALLBACK_URL": "https://callback.example.com",
            },
        )
        _assert_policy_has_actions(
            pulumi_mocks,
            "worker",
            "execute-api:ManageConnections",
            "lambda:InvokeFunction",
        )

    return function.invoke_arn.apply(check)
