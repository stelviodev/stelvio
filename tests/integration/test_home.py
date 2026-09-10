"""AwsHome against real AWS: the error spelling only the service itself can confirm."""

from pytest import mark, raises

from stelvio.aws.home import AwsHome
from stelvio.exceptions import StelvioValidationError

pytestmark = mark.integration


def test_read_param_with_bogus_keys_gets_friendly_error(hermetic_aws, monkeypatch):
    """A made-up access key reaches SSM and comes back as UnrecognizedClientException.

    Pins SSM's real error code and text; the unit tests stub the client and can only
    assume the spelling. Creates nothing: the request is rejected before it does anything.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFAKEFAKEFAKEFAKE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake" * 10)

    with raises(StelvioValidationError, match="UnrecognizedClientException") as exc:
        AwsHome(region="us-east-1").read_param("/stlv/bootstrap")

    assert str(exc.value) == (
        "An error occurred (UnrecognizedClientException) when calling the GetParameter "
        "operation: The security token included in the request is invalid.\n"
        "  Profile: default (AWS_PROFILE not set), region: us-east-1\n"
        "  Check your AWS setup: 'aws configure', 'aws sso login', AWS_PROFILE, "
        "or AwsConfig(profile=...) in stlv_app.py."
    )
