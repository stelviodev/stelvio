import pulumi

from stelvio.aws.cognito.user_pool import UserPool
from stelvio.aws.function import Function, FunctionConfig

from ...conftest import TP
from ..pulumi_mocks import ACCOUNT_ID, DEFAULT_REGION, R, tid, tn

POOL_ARN = (
    f"arn:aws:cognito-idp:{DEFAULT_REGION}:{ACCOUNT_ID}:userpool/"
    f"{DEFAULT_REGION}_{tid(f'{TP}users')}"
)
# The pre_sign_up trigger function's name in AWS, as the mock resolves it
PRE_SIGN_UP_FN_NAME = tn(f"{TP}users-trigger-pre_sign_up")
PRE_SIGN_UP_FN_ARN = f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:{PRE_SIGN_UP_FN_NAME}"

# =========================================================================
# Single trigger tests
# =========================================================================


def test_single_trigger_creates_permission(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-trigger-pre_sign_up-perm",
        R.LAMBDA_PERMISSION,
        {
            "action": "lambda:InvokeFunction",
            "function": PRE_SIGN_UP_FN_NAME,
            "principal": "cognito-idp.amazonaws.com",
            "sourceArn": POOL_ARN,
        },
    )
    pulumi_mocks.assert_res_counts(
        {
            R.USER_POOL: 1,
            R.FUNCTION: 1,
            R.ROLE: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
            R.LAMBDA_PERMISSION: 1,
        }
    )


# =========================================================================
# Multiple triggers tests
# =========================================================================


def test_multiple_triggers_create_a_function_and_permission_each(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": "functions/auth/validate.handler",
            "post_confirmation": "functions/auth/welcome.handler",
        },
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users-trigger-pre_sign_up", R.FUNCTION)
    pulumi_mocks.assert_res("users-trigger-post_confirmation", R.FUNCTION)
    pulumi_mocks.assert_res("users-trigger-pre_sign_up-perm", R.LAMBDA_PERMISSION)
    pulumi_mocks.assert_res("users-trigger-post_confirmation-perm", R.LAMBDA_PERMISSION)
    pulumi_mocks.assert_res_counts(
        {
            R.USER_POOL: 1,
            R.FUNCTION: 2,
            R.ROLE: 2,
            R.ROLE_POLICY_ATTACHMENT: 2,
            R.LAMBDA_PERMISSION: 2,
        }
    )


# =========================================================================
# Permission details tests
# =========================================================================


def test_trigger_permission_principal(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-trigger-pre_sign_up-perm",
        R.LAMBDA_PERMISSION,
        {"principal": "cognito-idp.amazonaws.com"},
        partial=True,
    )


def test_trigger_permission_source_arn(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-trigger-pre_sign_up-perm",
        R.LAMBDA_PERMISSION,
        {"sourceArn": POOL_ARN},
        partial=True,
    )


def test_trigger_permission_function_reference(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-trigger-pre_sign_up-perm",
        R.LAMBDA_PERMISSION,
        {"function": PRE_SIGN_UP_FN_NAME},
        partial=True,
    )


# =========================================================================
# Trigger function naming tests
# =========================================================================


def test_trigger_function_naming(pulumi_mocks, project_cwd):
    pool = UserPool(
        "auth",
        usernames=["email"],
        triggers={"post_authentication": "functions/auth/validate.handler"},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("auth-trigger-post_authentication", R.FUNCTION)


# =========================================================================
# Handler forms tests
# =========================================================================


def test_handler_string_form(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-trigger-pre_sign_up", R.FUNCTION, {"handler": "validate.handler"}, partial=True
    )


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

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-trigger-pre_sign_up",
        R.FUNCTION,
        {"handler": "validate.handler", "memorySize": 512, "timeout": 60},
        partial=True,
    )


def test_existing_function_form(pulumi_mocks, project_cwd):
    existing_fn = Function("my-validator", handler="functions/auth/validate.handler")
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": existing_fn},
    )

    @pulumi.runtime.test
    def deploy():
        return [pool.resources, existing_fn.resources]

    deploy()

    assert pool.resources.trigger_functions["pre_sign_up"] is existing_fn

    # Only the existing function, none with trigger naming
    pulumi_mocks.assert_res("my-validator", R.FUNCTION)
    pulumi_mocks.assert_res_counts(
        {
            R.USER_POOL: 1,
            R.FUNCTION: 1,
            R.ROLE: 1,
            R.ROLE_POLICY_ATTACHMENT: 1,
            R.LAMBDA_PERMISSION: 1,
        }
    )


def test_dict_handler_form(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": {"handler": "functions/auth/validate.handler", "memory": 256},
        },
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-trigger-pre_sign_up",
        R.FUNCTION,
        {"handler": "validate.handler", "memorySize": 256},
        partial=True,
    )


# =========================================================================
# Lambda config on pool tests
# =========================================================================


def test_lambda_config_maps_trigger_keys(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={
            "pre_sign_up": "functions/auth/validate.handler",
            "post_confirmation": "functions/auth/welcome.handler",
        },
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users",
        R.USER_POOL,
        {
            "lambdaConfig": {
                "preSignUp": PRE_SIGN_UP_FN_ARN,
                "postConfirmation": f"arn:aws:lambda:{DEFAULT_REGION}:{ACCOUNT_ID}:function:"
                f"{tn(f'{TP}users-trigger-post_confirmation')}",
            }
        },
        partial=True,
    )


def test_lambda_config_references_function_arn(pulumi_mocks, project_cwd):
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler"},
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users", R.USER_POOL, {"lambdaConfig": {"preSignUp": PRE_SIGN_UP_FN_ARN}}, partial=True
    )


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


def test_no_triggers_lambda_config_none(pulumi_mocks):
    pool = UserPool("users", usernames=["email"])

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res("users", R.USER_POOL, {"lambdaConfig": None}, partial=True)


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

    fns = pool.resources.trigger_functions
    pulumi.Output.all(
        fns["pre_sign_up"].function_name,
        fns["post_confirmation"].function_name,
    ).apply(check)


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

    perms = pool.resources.trigger_permissions
    pulumi.Output.all(
        perms["pre_sign_up"].id,
        perms["post_confirmation"].id,
    ).apply(check)


# =========================================================================
# Customize tests
# =========================================================================


def test_trigger_customize_reaches_built_functions_and_permissions(pulumi_mocks, project_cwd):
    own = Function("own", handler="functions/auth/validate.handler")
    pool = UserPool(
        "users",
        usernames=["email"],
        triggers={"pre_sign_up": "functions/auth/validate.handler", "post_confirmation": own},
        customize={
            "trigger_functions": {"function": {"memory_size": 1024}},
            "trigger_permissions": {"statement_id": "cognito"},
        },
    )

    @pulumi.runtime.test
    def deploy():
        return pool.resources

    deploy()

    pulumi_mocks.assert_res(
        "users-trigger-pre_sign_up", R.FUNCTION, {"memorySize": 1024}, partial=True
    )
    # A passed-in Function keeps its own customize
    pulumi_mocks.assert_res("own", R.FUNCTION, {"memorySize": 128}, partial=True)
    for trigger in ("pre_sign_up", "post_confirmation"):
        pulumi_mocks.assert_res(
            f"users-trigger-{trigger}-perm",
            R.LAMBDA_PERMISSION,
            {"statementId": "cognito"},
            partial=True,
        )
    pulumi_mocks.assert_res_counts(
        {
            R.USER_POOL: 1,
            R.FUNCTION: 2,
            R.ROLE: 2,
            R.ROLE_POLICY_ATTACHMENT: 2,
            R.LAMBDA_PERMISSION: 2,
        }
    )
