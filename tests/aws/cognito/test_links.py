import json

import pulumi

from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.function import Function

from ...conftest import TP


@pulumi.runtime.test
def test_user_pool_link_properties_are_injected_as_env_vars(
    pulumi_mocks,
    project_cwd,
    mock_get_or_install_dependencies_function,
):
    pool = UserPool("users", usernames=["email"])
    consumer = Function(
        "consumer",
        handler="functions/simple.handler",
        links=[pool],
    )

    def verify_env_vars(_):
        functions = pulumi_mocks.created_functions(f"{TP}consumer")
        assert len(functions) == 1

        env_vars = functions[0].inputs["environment"]["variables"]
        assert env_vars["STLV_USERS_USER_POOL_ID"] == f"{TP}users-test-id"
        assert env_vars["STLV_USERS_USER_POOL_ARN"].startswith("arn:aws:cognito-idp:")
        assert ":userpool/" in env_vars["STLV_USERS_USER_POOL_ARN"]

    return consumer.invoke_arn.apply(verify_env_vars)


@pulumi.runtime.test
def test_user_pool_link_permissions_grant_read_only_cognito_access(
    pulumi_mocks,
    project_cwd,
    mock_get_or_install_dependencies_function,
):
    pool = UserPool("users", usernames=["email"])
    consumer = Function(
        "consumer",
        handler="functions/simple.handler",
        links=[pool],
    )

    def verify_policy(_):
        policies = pulumi_mocks.created_policies(f"{TP}consumer-p")
        assert len(policies) == 1

        policy_content = json.loads(policies[0].inputs["policy"])
        assert len(policy_content) == 1

        statement = policy_content[0]
        assert sorted(statement["actions"]) == sorted(
            [
                "cognito-idp:GetUser",
                "cognito-idp:AdminGetUser",
                "cognito-idp:ListUsers",
            ]
        )
        assert len(statement["resources"]) == 1
        assert statement["resources"][0].startswith("arn:aws:cognito-idp:")
        assert ":userpool/" in statement["resources"][0]

    return consumer.invoke_arn.apply(verify_policy)


@pulumi.runtime.test
def test_user_pool_client_link_properties_are_injected_as_env_vars(
    pulumi_mocks,
    project_cwd,
    mock_get_or_install_dependencies_function,
):
    pool = UserPool("users")
    client = pool.add_client("web")
    _ = pool.resources
    consumer = Function(
        "consumer",
        handler="functions/simple.handler",
        links=[client],
    )

    def verify_env_vars(_):
        functions = pulumi_mocks.created_functions(f"{TP}consumer")
        assert len(functions) == 1

        env_vars = functions[0].inputs["environment"]["variables"]
        assert env_vars["STLV_USERS_WEB_CLIENT_ID"] == f"{TP}users-web-test-id"
        assert env_vars["STLV_USERS_WEB_USER_POOL_ID"] == f"{TP}users-test-id"

    return consumer.invoke_arn.apply(verify_env_vars)


@pulumi.runtime.test
def test_user_pool_client_link_with_secret_injects_client_secret_env_var(
    pulumi_mocks,
    project_cwd,
    mock_get_or_install_dependencies_function,
):
    pool = UserPool("users")
    client = pool.add_client("web", generate_secret=True)
    _ = pool.resources
    consumer = Function(
        "consumer",
        handler="functions/simple.handler",
        links=[client],
    )

    def verify_env_vars(_):
        functions = pulumi_mocks.created_functions(f"{TP}consumer")
        assert len(functions) == 1

        env_vars = functions[0].inputs["environment"]["variables"]
        assert env_vars["STLV_USERS_WEB_CLIENT_SECRET"] == f"secret-{TP}users-web-test-id"

    return consumer.invoke_arn.apply(verify_env_vars)


@pulumi.runtime.test
def test_user_pool_client_link_without_secret_omits_client_secret_env_var(
    pulumi_mocks,
    project_cwd,
    mock_get_or_install_dependencies_function,
):
    pool = UserPool("users")
    client = pool.add_client("web", generate_secret=False)
    _ = pool.resources
    consumer = Function(
        "consumer",
        handler="functions/simple.handler",
        links=[client],
    )

    def verify_env_vars(_):
        functions = pulumi_mocks.created_functions(f"{TP}consumer")
        assert len(functions) == 1

        env_vars = functions[0].inputs["environment"]["variables"]
        assert "STLV_USERS_WEB_CLIENT_SECRET" not in env_vars

    return consumer.invoke_arn.apply(verify_env_vars)


@pulumi.runtime.test
def test_user_pool_client_link_permissions_match_user_pool_read_only_permissions(
    pulumi_mocks,
    project_cwd,
    mock_get_or_install_dependencies_function,
):
    pool = UserPool("users")
    client = pool.add_client("web")
    _ = pool.resources
    consumer = Function(
        "consumer",
        handler="functions/simple.handler",
        links=[client],
    )

    def verify_policy(_):
        policies = pulumi_mocks.created_policies(f"{TP}consumer-p")
        assert len(policies) == 1

        policy_content = json.loads(policies[0].inputs["policy"])
        assert len(policy_content) == 1

        statement = policy_content[0]
        assert sorted(statement["actions"]) == sorted(
            [
                "cognito-idp:GetUser",
                "cognito-idp:AdminGetUser",
                "cognito-idp:ListUsers",
            ]
        )
        assert len(statement["resources"]) == 1
        assert statement["resources"][0].startswith("arn:aws:cognito-idp:")
        assert ":userpool/" in statement["resources"][0]

    return consumer.invoke_arn.apply(verify_policy)
