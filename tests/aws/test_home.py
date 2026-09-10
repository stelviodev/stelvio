"""AwsHome through its public surface: a wrong or missing AWS setup gives one short
message with the profile and a fix; anything else keeps its original exception."""

from botocore.exceptions import ClientError, NoCredentialsError
from botocore.stub import Stubber
from pytest import fixture, mark, param, raises

from stelvio.aws.home import AwsHome
from stelvio.exceptions import StelvioValidationError

# The two fix lines are shared by the whole error family on purpose (design decision:
# botocore's own text is the headline, the fix is generic), so they live here once.
SETUP_FIX = (
    "Check your AWS setup: 'aws configure', 'aws sso login', AWS_PROFILE, "
    "or AwsConfig(profile=...) in stlv_app.py."
)
PERMISSIONS_FIX = (
    "Stelvio needs SSM Parameter Store (/stlv/*) and S3 (stlv-state-* bucket) "
    "access for its state home."
)


@fixture
def home(hermetic_aws):
    return AwsHome(region="us-east-1")


def _aws_rejects(home: AwsHome, code: str) -> Stubber:
    """Make the next SSM call fail with `code`, the way the real service would."""
    stub = Stubber(home._ssm)
    stub.add_client_error("get_parameter", service_error_code=code, service_message="rejected")
    return stub


# --- Missing or wrong setup: caught at the first AWS call of every command ---


def test_read_param_without_credentials_raises_friendly_error(hermetic_aws):
    with raises(StelvioValidationError, match="Unable to locate credentials") as exc:
        AwsHome(region="us-east-1").read_param("/stlv/bootstrap")

    assert str(exc.value) == (
        "Unable to locate credentials\n"
        "  Profile: default (AWS_PROFILE not set), region: us-east-1\n"
        f"  {SETUP_FIX}"
    )


def test_configured_profile_not_found_raises_friendly_error(hermetic_aws):
    """AwsConfig(profile=...) names a profile the machine doesn't have."""
    with raises(StelvioValidationError, match="could not be found") as exc:
        AwsHome(profile="nope", region="us-east-1")

    assert str(exc.value) == (
        "The config profile (nope) could not be found\n"
        "  Profile: 'nope' (AwsConfig in stlv_app.py), region: us-east-1\n"
        "  Check your AWS setup: 'aws configure', 'aws sso login --profile nope', "
        "AWS_PROFILE, or AwsConfig(profile=...) in stlv_app.py."
    )


@mark.parametrize(
    ("env", "message"),
    [
        param(
            {"AWS_PROFILE": "nope"},
            "The config profile (nope) could not be found\n"
            "  Profile: 'nope' (AWS_PROFILE), region: us-east-1\n"
            "  Check your AWS setup: 'aws configure', 'aws sso login --profile nope', "
            "AWS_PROFILE, or AwsConfig(profile=...) in stlv_app.py.",
            id="unknown",
        ),
        param(
            {"AWS_PROFILE": ""},
            "The config profile () could not be found\n"
            "  Profile: '' (AWS_PROFILE is set but empty), region: us-east-1\n"
            f"  {SETUP_FIX}",
            id="empty",
        ),
        param(
            {"AWS_DEFAULT_PROFILE": "nope", "AWS_PROFILE": "other"},
            "The config profile (nope) could not be found\n"
            "  Profile: 'nope' (AWS_DEFAULT_PROFILE), region: us-east-1\n"
            "  Check your AWS setup: 'aws configure', 'aws sso login --profile nope', "
            "AWS_PROFILE, or AwsConfig(profile=...) in stlv_app.py.",
            id="default-profile-wins",
        ),
    ],
)
def test_profile_env_var_not_found_raises_friendly_error(hermetic_aws, monkeypatch, env, message):
    """The message names the variable the profile came from — botocore's text never does.

    Two traps: an empty AWS_PROFILE is a profile named '' to botocore, and
    AWS_DEFAULT_PROFILE takes precedence over AWS_PROFILE.
    """
    for var, value in env.items():
        monkeypatch.setenv(var, value)

    with raises(StelvioValidationError, match="could not be found") as exc:
        AwsHome(region="us-east-1")

    assert str(exc.value) == message


def test_read_param_with_partial_credentials_raises_friendly_error(hermetic_aws, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKEFAKE")

    with raises(StelvioValidationError, match="Partial credentials") as exc:
        AwsHome(region="us-east-1").read_param("/stlv/bootstrap")

    assert str(exc.value) == (
        "Partial credentials found in env, missing: AWS_SECRET_ACCESS_KEY\n"
        "  Profile: default (AWS_PROFILE not set), region: us-east-1\n"
        f"  {SETUP_FIX}"
    )


def test_sso_profile_without_cached_token_raises_friendly_error(
    hermetic_aws, monkeypatch, tmp_path
):
    """The fix line names the profile to log in with. botocore only fails at the first call,
    not when the client is built, so read_param is the site that catches it."""
    config = tmp_path / "config"
    config.write_text(
        "[profile sso-dev]\n"
        "sso_session = corp\n"
        "sso_account_id = 123456789012\n"
        "sso_role_name = Dev\n"
        "\n"
        "[sso-session corp]\n"
        "sso_start_url = https://corp.awsapps.com/start\n"
        "sso_region = us-east-1\n"
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))

    with raises(StelvioValidationError, match="SSO Token") as exc:
        AwsHome(profile="sso-dev", region="us-east-1").read_param("/stlv/bootstrap")

    assert str(exc.value) == (
        "Error loading SSO Token: Token for corp does not exist\n"
        "  Profile: 'sso-dev' (AwsConfig in stlv_app.py), region: us-east-1\n"
        "  Check your AWS setup: 'aws configure', 'aws sso login --profile sso-dev', "
        "AWS_PROFILE, or AwsConfig(profile=...) in stlv_app.py."
    )


@mark.parametrize(
    ("code", "fix"),
    [
        param("ExpiredTokenException", SETUP_FIX, id="expired-token"),
        param("UnrecognizedClientException", SETUP_FIX, id="unknown-access-key"),
        param("InvalidSignatureException", SETUP_FIX, id="wrong-secret-key"),
        param("AccessDeniedException", PERMISSIONS_FIX, id="access-denied"),
    ],
)
def test_read_param_rejected_by_aws_raises_friendly_error(home, code, fix):
    """SSM's own error codes for bad credentials or missing permissions."""
    with _aws_rejects(home, code), raises(StelvioValidationError, match=code) as exc:
        home.read_param("/stlv/bootstrap")

    assert str(exc.value) == (
        f"An error occurred ({code}) when calling the GetParameter operation: rejected\n"
        "  Profile: default (AWS_PROFILE not set), region: us-east-1\n"
        f"  {fix}"
    )


# --- Everything else stays as it was ---


def test_read_param_other_aws_error_keeps_original_exception(home):
    """Not a setup problem, so the traceback must stay useful."""
    with (
        _aws_rejects(home, "ThrottlingException"),
        raises(ClientError, match="ThrottlingException"),
    ):
        home.read_param("/stlv/bootstrap")


def test_stlv_debug_keeps_original_exception(hermetic_aws, monkeypatch):
    monkeypatch.setenv("STLV_DEBUG", "1")

    with raises(NoCredentialsError, match="Unable to locate credentials"):
        AwsHome(region="us-east-1").read_param("/stlv/bootstrap")


def test_read_param_missing_parameter_returns_none(home):
    with _aws_rejects(home, "ParameterNotFound"):
        assert home.read_param("/stlv/bootstrap") is None
