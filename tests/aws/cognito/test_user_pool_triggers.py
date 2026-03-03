import pulumi

from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.function import Function, FunctionConfig

from ...conftest import TP


@pulumi.runtime.test
def test_user_pool_single_trigger_creates_function_and_permission(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    def check_resources(_):
        assert len(pool.resources.trigger_functions) == 1
        assert len(pool.resources.trigger_permissions) == 1

        fn = pulumi_mocks.assert_function_created(f"{TP}users-trigger-pre_sign_up")
        assert fn.inputs["handler"] == "validate.handler"

        permissions = pulumi_mocks.created_permissions()
        assert len(permissions) == 1
        perm = permissions[0]
        assert perm.inputs["principal"] == "cognito-idp.amazonaws.com"

    permission_outputs = [
        permission.id for permission in pool.resources.trigger_permissions.values()
    ]
    return pulumi.Output.all(pool.id, *permission_outputs).apply(check_resources)


@pulumi.runtime.test
def test_user_pool_multiple_triggers_create_function_and_permission_per_trigger(
    pulumi_mocks, project_cwd
):
    pool = UserPool(
        "users",
        triggers={
            "pre_sign_up": "functions/auth/validate.handler",
            "post_confirmation": "functions/auth/welcome.handler",
        },
    )

    def check_resources(_):
        assert len(pool.resources.trigger_functions) == 2
        assert len(pool.resources.trigger_permissions) == 2

        assert len(pulumi_mocks.created_functions(f"{TP}users-trigger-pre_sign_up")) == 1
        assert len(pulumi_mocks.created_functions(f"{TP}users-trigger-post_confirmation")) == 1

        permissions = pulumi_mocks.created_permissions()
        assert len(permissions) == 2

    permission_outputs = [
        permission.id for permission in pool.resources.trigger_permissions.values()
    ]
    return pulumi.Output.all(pool.id, *permission_outputs).apply(check_resources)


@pulumi.runtime.test
def test_user_pool_trigger_permission_principal_is_cognito(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        triggers={"custom_message": "functions/auth/welcome.handler"},
    )

    def check_resources(_):
        permissions = pulumi_mocks.created_permissions()
        assert len(permissions) == 1
        permission = permissions[0]
        assert permission.inputs["principal"] == "cognito-idp.amazonaws.com"

    permission_outputs = [
        permission.id for permission in pool.resources.trigger_permissions.values()
    ]
    return pulumi.Output.all(pool.id, *permission_outputs).apply(check_resources)


@pulumi.runtime.test
def test_user_pool_trigger_permission_source_arn_references_pool(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        triggers={"pre_authentication": "functions/auth/validate.handler"},
    )

    def check_resources(args):
        pool_arn = args[0]
        permissions = pulumi_mocks.created_permissions()
        assert len(permissions) == 1
        permission = permissions[0]
        assert permission.inputs["sourceArn"] == pool_arn

    permission_outputs = [
        permission.id for permission in pool.resources.trigger_permissions.values()
    ]
    return pulumi.Output.all(pool.arn, pool.id, *permission_outputs).apply(check_resources)


@pulumi.runtime.test
def test_user_pool_trigger_function_name_matches_pattern(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        triggers={"post_authentication": "functions/auth/welcome.handler"},
    )

    def check_resources(_):
        fn = pulumi_mocks.assert_function_created(f"{TP}users-trigger-post_authentication")
        assert fn.typ == "aws:lambda/function:Function"

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_trigger_with_handler_string_creates_new_function(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        triggers={"post_confirmation": "functions/auth/welcome.handler"},
    )

    def check_resources(_):
        fn = pulumi_mocks.assert_function_created(f"{TP}users-trigger-post_confirmation")
        assert fn.inputs["handler"] == "welcome.handler"

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_trigger_with_function_config_creates_new_function(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        triggers={
            "pre_sign_up": FunctionConfig(
                handler="functions/auth/validate.handler",
                memory=512,
            )
        },
    )

    def check_resources(_):
        fn = pulumi_mocks.assert_function_created(f"{TP}users-trigger-pre_sign_up")
        assert fn.inputs["memorySize"] == 512

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_trigger_with_existing_function_reuses_function(pulumi_mocks, project_cwd):
    existing = Function("existing-trigger", handler="functions/auth/validate.handler")
    pool = UserPool("users", triggers={"pre_sign_up": existing})

    def check_resources(_):
        assert len(pulumi_mocks.created_functions(f"{TP}existing-trigger")) == 1
        assert len(pulumi_mocks.created_functions(f"{TP}users-trigger-pre_sign_up")) == 0

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_lambda_config_maps_trigger_keys(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        triggers={
            "pre_sign_up": "functions/auth/validate.handler",
            "custom_message": "functions/auth/welcome.handler",
        },
    )

    def check_resources(_):
        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        lambda_config = user_pool.inputs["lambdaConfig"]

        assert "preSignUp" in lambda_config
        assert "customMessage" in lambda_config
        assert lambda_config["preSignUp"].startswith("arn:aws:lambda:")
        assert lambda_config["customMessage"].startswith("arn:aws:lambda:")

    return pool.id.apply(check_resources)


@pulumi.runtime.test
def test_user_pool_without_triggers_has_no_trigger_resources_or_lambda_config(pulumi_mocks):
    pool = UserPool("users")

    def check_resources(_):
        assert pool.resources.trigger_functions == {}
        assert pool.resources.trigger_permissions == {}

        user_pool = pulumi_mocks.assert_user_pool_created(f"{TP}users")
        assert user_pool.inputs.get("lambdaConfig") is None

    return pool.id.apply(check_resources)
