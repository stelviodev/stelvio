from tests.cli_test_helpers import FakeCommandRun


def _state_with_grouped_resources() -> dict:
    stack_urn = "urn:pulumi:dev::myapp::pulumi:pulumi:Stack::myapp-dev"
    component_urn = "urn:pulumi:dev::myapp::stelvio:aws:Function::api"
    lambda_urn = "urn:pulumi:dev::myapp::aws:lambda/function:Function::myapp-dev-api"
    role_urn = "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-api-r"
    provider_urn = "urn:pulumi:dev::myapp::pulumi:providers:aws::default_6_78_0"
    return {
        "checkpoint": {
            "latest": {
                "resources": [
                    {"urn": stack_urn, "type": "pulumi:pulumi:Stack"},
                    {
                        "urn": component_urn,
                        "type": "stelvio:aws:Function",
                        "parent": stack_urn,
                    },
                    {
                        "urn": lambda_urn,
                        "type": "aws:lambda/function:Function",
                        "parent": component_urn,
                        "dependencies": [role_urn],
                    },
                    {
                        "urn": role_urn,
                        "type": "aws:iam/role:Role",
                        "parent": component_urn,
                    },
                    {"urn": provider_urn, "type": "pulumi:providers:aws"},
                ]
            }
        }
    }


def test_state_list_command_accepts_json_flag(cli) -> None:
    result = cli.state_list.main(["--env", "dev", "--json"], standalone_mode=False)

    assert result is None
    cli.run_state_list.assert_called_once_with("dev", json_output=True, show_outputs=False)


def test_state_list_command_defaults_to_personal_env(cli, monkeypatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setattr(cli, "get_user_env", lambda: "alice")

    result = cli.state_list.main(["--json"], standalone_mode=False)

    assert result is None
    cli.run_state_list.assert_called_once_with("alice", json_output=True, show_outputs=False)


def test_run_state_list_prints_grouped_tree_in_human_mode(cli_commands) -> None:
    cli_commands.CommandRun.return_value = FakeCommandRun(
        _state_with_grouped_resources(), app_name="myapp"
    )

    cli_commands.run_state_list("dev")

    assert cli_commands.console.lines == [
        "[bold]Resources (5):[/bold]\n",
        "[bold]Stack[/bold] myapp-dev",
        "  [bold]Function[/bold] api",
        "    Type: stelvio:aws:Function",
        "    [cyan]myapp-dev-api[/cyan]",
        "      Type: aws:lambda/function:Function",
        "      Depends on: myapp-dev-api-r",
        "    [cyan]myapp-dev-api-r[/cyan]",
        "      Type: aws:iam/role:Role",
        "",
        "[bold]Providers[/bold]",
        "  [cyan]default_6_78_0[/cyan]",
        "    Type: pulumi:providers:aws",
    ]


def test_run_state_list_prints_grouped_json(cli_commands) -> None:
    cli_commands.CommandRun.return_value = FakeCommandRun(
        _state_with_grouped_resources(), app_name="myapp"
    )

    cli_commands.run_state_list("dev", json_output=True)

    cli_commands.console.print_json.assert_called_once_with(
        data={
            "stack": {
                "name": "myapp-dev",
                "urn": "urn:pulumi:dev::myapp::pulumi:pulumi:Stack::myapp-dev",
                "type": "pulumi:pulumi:Stack",
                "parent": None,
                "dependencies": [],
            },
            "components": [
                {
                    "name": "api",
                    "urn": "urn:pulumi:dev::myapp::stelvio:aws:Function::api",
                    "type": "stelvio:aws:Function",
                    "parent": "urn:pulumi:dev::myapp::pulumi:pulumi:Stack::myapp-dev",
                    "dependencies": [],
                    "component_type": "Function",
                    "children": [
                        {
                            "name": "myapp-dev-api",
                            "urn": (
                                "urn:pulumi:dev::myapp::aws:lambda/function:Function::"
                                "myapp-dev-api"
                            ),
                            "type": "aws:lambda/function:Function",
                            "parent": "urn:pulumi:dev::myapp::stelvio:aws:Function::api",
                            "dependencies": [
                                "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-api-r"
                            ],
                            "children": [],
                        },
                        {
                            "name": "myapp-dev-api-r",
                            "urn": "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-api-r",
                            "type": "aws:iam/role:Role",
                            "parent": "urn:pulumi:dev::myapp::stelvio:aws:Function::api",
                            "dependencies": [],
                            "children": [],
                        },
                    ],
                }
            ],
            "providers": [
                {
                    "name": "default_6_78_0",
                    "urn": "urn:pulumi:dev::myapp::pulumi:providers:aws::default_6_78_0",
                    "type": "pulumi:providers:aws",
                    "parent": None,
                    "dependencies": [],
                    "children": [],
                }
            ],
        }
    )


def test_run_state_list_json_with_empty_state_returns_structured_empty_json(cli_commands) -> None:
    cli_commands.run_state_list("dev", json_output=True)

    cli_commands.console.print_json.assert_called_once_with(data={"components": []})


def test_run_state_list_json_with_no_deployed_app_returns_structured_empty_json(
    cli_commands,
) -> None:
    cli_commands.CommandRun.return_value.has_deployed = False

    cli_commands.run_state_list("dev", json_output=True)

    assert cli_commands.console.lines == []
    cli_commands.console.print_json.assert_called_once_with(data={"components": []})


def test_run_state_list_wraps_long_dependency_lines_with_tree_indent(cli_commands) -> None:
    state = _state_with_grouped_resources()
    state["checkpoint"]["latest"]["resources"][2]["dependencies"] = [
        "urn:pulumi:dev::myapp::aws:iam/role:Role::myapp-dev-api-r",
        "urn:pulumi:dev::myapp::aws:iam/policy:Policy::myapp-dev-api-p",
        "urn:pulumi:dev::myapp::aws:sqs/queue:Queue::myapp-dev-tasks",
    ]
    cli_commands.CommandRun.return_value = FakeCommandRun(state, app_name="myapp")
    cli_commands.console.size.width = 45

    cli_commands.run_state_list("dev")

    dependency_lines = [
        line
        for line in cli_commands.console.lines
        if "Depends on:" in line or "myapp-dev-tasks" in line
    ]
    assert dependency_lines[0].startswith("      Depends on: ")
    assert len(dependency_lines) > 1
    assert all(line.startswith("                  ") for line in dependency_lines[1:])
