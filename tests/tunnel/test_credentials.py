"""A real separate interpreter selects the captured SDK source, not handler inputs."""

import os
import subprocess
import sys

from pytest import mark, raises

from stelvio.tunnel.credentials import AwsExecutionContext

WORKER = r"""
import os,sys
from types import MappingProxyType
from botocore.stub import Stubber
from stelvio.tunnel import credentials as module
real_factory=module.boto3.Session
stubs=[]
failure=sys.argv[5]
def factory(**kwargs):
    session=real_factory(**kwargs)
    sts=session.client("sts",config=module.AWS_IO)
    stub=Stubber(sts)
    response={"Account":"123456789012","UserId":"fixture","Arn":"arn:aws:iam::123456789012:user/fixture"}
    initial=dict(response,Account="999999999999") if failure=="initial-account" else response
    stub.add_response("get_caller_identity",initial,{})
    if failure in ("none","refresh-account"):
        refreshed=dict(response,Account="999999999999") if failure=="refresh-account" else response
        stub.add_response("get_caller_identity",refreshed,{})
    stub.activate();stubs.append(stub)
    original=session.client
    session.client=lambda service,**config: sts if service=="sts" else original(service,**config)
    return session
module.boto3.Session=factory
context=module.AwsExecutionContext("provider", "123456789012", "us-east-1",
    None if sys.argv[2]=="-" else sys.argv[2],os.geteuid(),int(sys.argv[1]),
    MappingProxyType(dict(os.environ)))
if failure=="initial-account":
    try: context.open_session()
    except RuntimeError as error:
        assert str(error)=="Captured AWS credential source belongs to another account"
    else: raise AssertionError("accepted foreign initial account")
    assert context._session is None
    for stub in stubs: stub.assert_no_pending_responses()
    print("credential boundary refused");sys.exit(0)
session=context.open_session()
assert context.open_session() is session
if failure!="none":
    if failure=="child-environment": os.environ["AWS_ACCESS_KEY_ID"]="CHILD_MUTATION"
    try:
        context.frozen_credentials(real_factory() if failure=="foreign-session" else session)
    except RuntimeError as error:
        expected={
            "refresh-account":"Refreshed AWS credential source belongs to another account",
            "foreign-session":"Credential refresh received another provider's SDK session",
            "child-environment":"AWS runtime was launched with a different credential context",
        }
        assert str(error)==expected[failure]
    else: raise AssertionError("accepted invalid credential boundary")
    if failure=="child-environment": os.environ["AWS_ACCESS_KEY_ID"]=sys.argv[3]
    assert context.open_session() is session
    for stub in stubs: stub.assert_no_pending_responses()
    print("credential boundary refused");sys.exit(0)
credentials=context.frozen_credentials(session)
assert credentials.access_key==sys.argv[3]
assert credentials.secret_key=="public-test-secret"
assert credentials.token=="public-test-token"
assert session.get_credentials().method==sys.argv[4]
assert "public-test-secret" not in repr(context)
assert "public-test-secret" not in repr(credentials)
for stub in stubs: stub.assert_no_pending_responses()
print("captured source and cached provider preserved")
"""


@mark.parametrize(
    ("profile", "failure"),
    [
        (None, "none"),
        ("isolated-profile", "none"),
        (None, "initial-account"),
        (None, "refresh-account"),
        (None, "foreign-session"),
        (None, "child-environment"),
    ],
)
def test_actual_child_sdk_uses_startup_source_after_handler_environment_changes(
    tmp_path, monkeypatch, profile, failure
):
    credentials_file = tmp_path / "credentials"
    credentials_file.write_text(
        "[isolated-profile]\naws_access_key_id=PROFILE_TEST_KEY\n"
        "aws_secret_access_key=public-test-secret\naws_session_token=public-test-token\n"
    )
    config = tmp_path / "config"
    config.write_text("")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "STARTUP_TEST_KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "public-test-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "public-test-token")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials_file))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    context = AwsExecutionContext.capture(
        provider="provider", account="123456789012", region="us-east-1", profile=profile
    )
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "HANDLER_KEY")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "HANDLER_SECRET")
    monkeypatch.setenv("AWS_PROFILE", "HANDLER_PROFILE")
    monkeypatch.setenv("AWS_CONFIG_FILE", "/not/the/original/file")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    expected = "STARTUP_TEST_KEY" if profile is None else "PROFILE_TEST_KEY"
    method = "env" if profile is None else "shared-credentials-file"
    result = subprocess.run(  # noqa: S603 - fixed test interpreter/script, public fake credentials
        [
            sys.executable,
            "-I",
            "-c",
            WORKER,
            str(os.getpid()),
            profile or "-",
            expected,
            method,
            failure,
        ],
        env=context.process_environment(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    expected_output = (
        "captured source and cached provider preserved"
        if failure == "none"
        else "credential boundary refused"
    )
    assert result.stdout.strip() == expected_output
    assert "public-test-secret" not in repr(context)
    environment = context.process_environment()
    environment["AWS_SECRET_ACCESS_KEY"] = "caller mutated copy"  # noqa: S105 - public fixture
    assert context.process_environment()["AWS_SECRET_ACCESS_KEY"] == "public-test-secret"  # noqa: S105


def test_parent_cannot_open_sdk_session_even_before_handler_mutation():
    context = AwsExecutionContext.capture(
        provider="provider", account="123456789012", region="us-east-1"
    )
    with raises(RuntimeError, match="separate nonroot runtime"):
        context.open_session()
