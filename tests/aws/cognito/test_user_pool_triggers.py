import pulumi

from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.function import Function, FunctionConfig

from ...conftest import TP

# =========================================================================
# Single trigger tests
# =========================================================================


@pulumi.runtime.test
def test_single_trigger_creates_function(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    def check(_):
        fn_name = f"{TP}users-trigger-pre_sign_up"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        assert functions[0].typ == "aws:lambda/function:Function"
        assert functions[0].inputs["handler"] == "validate.handler"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_single_trigger_creates_permission(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    def check(args):
        pool_arn, _ = args
        perm_name = f"{TP}users-trigger-pre_sign_up-perm"
        permissions = pulumi_mocks.created_permissions(perm_name)
        assert len(permissions) == 1
        perm = permissions[0]
        assert perm.typ == "aws:lambda/permission:Permission"
        assert perm.inputs["action"] == "lambda:InvokeFunction"
        assert perm.inputs["principal"] == "cognito-idp.amazonaws.com"
        assert perm.inputs["sourceArn"] == pool_arn

    perm = pool.resources.trigger_permissions["pre_sign_up"]
    pulumi.Output.all(pool.arn, perm.id).apply(check)


# =========================================================================
# Multiple triggers tests
# =========================================================================


@pulumi.runtime.test
def test_multiple_triggers_create_functions(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": "functions/auth/validate.handler",
            "post_confirmation": "functions/auth/welcome.handler",
        },
    )

    def check(_):
        pre_fn = pulumi_mocks.created_functions(f"{TP}users-trigger-pre_sign_up")
        post_fn = pulumi_mocks.created_functions(f"{TP}users-trigger-post_confirmation")
        assert len(pre_fn) == 1
        assert len(post_fn) == 1

    pool.arn.apply(check)


@pulumi.runtime.test
def test_multiple_triggers_create_permissions(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": "functions/auth/validate.handler",
            "post_confirmation": "functions/auth/welcome.handler",
        },
    )

    def check(_):
        pre_perm = pulumi_mocks.created_permissions(f"{TP}users-trigger-pre_sign_up-perm")
        post_perm = pulumi_mocks.created_permissions(f"{TP}users-trigger-post_confirmation-perm")
        assert len(pre_perm) == 1
        assert len(post_perm) == 1

    perms = pool.resources.trigger_permissions
    pulumi.Output.all(perms["pre_sign_up"].id, perms["post_confirmation"].id).apply(check)


# =========================================================================
# Permission details tests
# =========================================================================


@pulumi.runtime.test
def test_trigger_permission_principal(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    def check(_):
        perm = pulumi_mocks.created_permissions(f"{TP}users-trigger-pre_sign_up-perm")[0]
        assert perm.inputs["principal"] == "cognito-idp.amazonaws.com"

    pool.resources.trigger_permissions["pre_sign_up"].id.apply(check)


@pulumi.runtime.test
def test_trigger_permission_source_arn(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    def check(args):
        pool_arn, _ = args
        perm = pulumi_mocks.created_permissions(f"{TP}users-trigger-pre_sign_up-perm")[0]
        assert perm.inputs["sourceArn"] == pool_arn

    perm = pool.resources.trigger_permissions["pre_sign_up"]
    pulumi.Output.all(pool.arn, perm.id).apply(check)


@pulumi.runtime.test
def test_trigger_permission_function_reference(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    def check(args):
        fn_name, _ = args
        perm = pulumi_mocks.created_permissions(f"{TP}users-trigger-pre_sign_up-perm")[0]
        assert perm.inputs["function"] == fn_name

    fn = pool.resources.trigger_functions["pre_sign_up"]
    perm = pool.resources.trigger_permissions["pre_sign_up"]
    pulumi.Output.all(fn.function_name, perm.id).apply(check)


# =========================================================================
# Trigger function naming tests
# =========================================================================


@pulumi.runtime.test
def test_trigger_function_naming(pulumi_mocks, project_cwd):
    pool = UserPool(
        "auth",
        usernames=["email"],
        triggers={"post_authentication": "functions/auth/validate.handler"},
    )

    def check(_):
        fn_name = f"{TP}auth-trigger-post_authentication"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1

    pool.arn.apply(check)


# =========================================================================
# Handler forms tests
# =========================================================================


@pulumi.runtime.test
def test_handler_string_form(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    def check(_):
        fn_name = f"{TP}users-trigger-pre_sign_up"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        assert functions[0].inputs["handler"] == "validate.handler"

    pool.arn.apply(check)


@pulumi.runtime.test
def test_function_config_form(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": FunctionConfig(
                handler="functions/auth/validate.handler",
                memory=512,
                timeout=60,
            ),
        },
    )

    def check(_):
        fn_name = f"{TP}users-trigger-pre_sign_up"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        fn = functions[0]
        assert fn.inputs["handler"] == "validate.handler"
        assert fn.inputs["memorySize"] == 512
        assert fn.inputs["timeout"] == 60

    pool.arn.apply(check)


@pulumi.runtime.test
def test_existing_function_form(pulumi_mocks, project_cwd):
    existing_fn = Function("my-validator", handler="functions/auth/validate.handler")
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": existing_fn},
    )

    def check(_):
        # The existing function should be reused — no extra function created for the trigger
        trigger_fn = pool.resources.trigger_functions["pre_sign_up"]
        assert trigger_fn is existing_fn

        # Only one function with the existing name, none with trigger naming
        existing_fns = pulumi_mocks.created_functions(f"{TP}my-validator")
        assert len(existing_fns) == 1
        trigger_fns = pulumi_mocks.created_functions(f"{TP}users-trigger-pre_sign_up")
        assert len(trigger_fns) == 0

    pool.arn.apply(check)


@pulumi.runtime.test
def test_dict_handler_form(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": {"handler": "functions/auth/validate.handler", "memory": 256},
        },
    )

    def check(_):
        fn_name = f"{TP}users-trigger-pre_sign_up"
        functions = pulumi_mocks.created_functions(fn_name)
        assert len(functions) == 1
        fn = functions[0]
        assert fn.inputs["handler"] == "validate.handler"
        assert fn.inputs["memorySize"] == 256

    pool.arn.apply(check)


# =========================================================================
# Lambda config on pool tests
# =========================================================================


@pulumi.runtime.test
def test_lambda_config_set_on_pool(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        lambda_config = mock.inputs["lambdaConfig"]
        assert lambda_config is not None
        assert "preSignUp" in lambda_config

    pool.arn.apply(check)


@pulumi.runtime.test
def test_lambda_config_maps_trigger_keys(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": "functions/auth/validate.handler",
            "post_confirmation": "functions/auth/welcome.handler",
        },
    )

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        lambda_config = mock.inputs["lambdaConfig"]
        assert "preSignUp" in lambda_config
        assert "postConfirmation" in lambda_config

    pool.arn.apply(check)


@pulumi.runtime.test
def test_lambda_config_references_function_arn(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    def check(fn_arn):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        lambda_config = mock.inputs["lambdaConfig"]
        assert lambda_config["preSignUp"] == fn_arn

    pulumi.Output.all(
        pool.resources.trigger_functions["pre_sign_up"].resources.function.arn,
        pool.arn,
    ).apply(lambda args: check(args[0]))


# =========================================================================
# No triggers tests
# =========================================================================


@pulumi.runtime.test
def test_no_triggers_empty_dicts(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        assert pool.resources.trigger_functions == {}
        assert pool.resources.trigger_permissions == {}

    pool.arn.apply(check)


@pulumi.runtime.test
def test_no_triggers_lambda_config_none(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    def check(_):
        mock = pulumi_mocks.assert_user_pool_created(TP + "users")
        assert mock.inputs.get("lambdaConfig") is None

    pool.arn.apply(check)


# =========================================================================
# Resources dataclass tests
# =========================================================================


@pulumi.runtime.test
def test_trigger_functions_in_resources(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": "functions/auth/validate.handler",
            "post_confirmation": "functions/auth/welcome.handler",
        },
    )

    def check(_):
        assert "pre_sign_up" in pool.resources.trigger_functions
        assert "post_confirmation" in pool.resources.trigger_functions
        assert len(pool.resources.trigger_functions) == 2

    pool.arn.apply(check)


@pulumi.runtime.test
def test_trigger_permissions_in_resources(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": "functions/auth/validate.handler",
            "post_confirmation": "functions/auth/welcome.handler",
        },
    )

    def check(_):
        assert "pre_sign_up" in pool.resources.trigger_permissions
        assert "post_confirmation" in pool.resources.trigger_permissions
        assert len(pool.resources.trigger_permissions) == 2

    pool.arn.apply(check)
