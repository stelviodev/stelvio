"""Actual default non-VPC dev, run after helper uninstall; destroy the owned app."""

# Proof assertions and JSON stdout are the acceptance interface.
# ruff: noqa: S101, T201
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from stelvio.tunnel.installation import INSTALLED_HELPER
from tests.integration.assert_tunnel import assert_application_absent, invoke_url
from tests.integration.cli_session import DESTROY_SECONDS, STARTUP_SECONDS, CliSession
from tests.integration.tunnel_cleanup import purge_test_metadata

assert not INSTALLED_HELPER.exists()
assert not Path("/Library/Application Support/Stelvio/tunnel").exists()
os.environ["STLV_TEST_AWS_PROFILE"] = "default"
os.environ["STLV_TEST_AWS_REGION"] = "us-east-1"
session = CliSession()
assert session.account == "535368238919"
source = f"""from stelvio.app import StelvioApp
from stelvio.config import StelvioAppConfig, AwsConfig
from stelvio.aws.function import Function
app=StelvioApp({session.app!r})
@app.config
def configuration(env):
    return StelvioAppConfig(aws={session.provider_source()})
@app.run
def run():
    Function("plain", handler="functions/plain.handler", url="public")
"""
(session.project / "stlv_app.py").write_text(source)
(session.project / "functions/plain.py").write_text(
    "import json,os\n"
    "def handler(event,context):\n"
    "    return {'statusCode':200,'body':json.dumps({'plain':True,"
    "'marker':os.environ['STLV_P6_MARKER'],'pid':os.getpid(),'uid':os.geteuid()})}\n"
)
failures = []
try:
    session.start()
    deadline = time.monotonic() + STARTUP_SECONDS
    while "local dev server connected to AppSync" not in session.log.read_text():
        assert session.process.poll() is None
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Plain CLI startup expired; retain {session.project}")
        time.sleep(1)
    url = session.url()
    expected = {
        "status": 200,
        "body": {
            "plain": True,
            "marker": session.marker,
            "pid": session.process.pid,
            "uid": os.geteuid(),
        },
    }
    assert invoke_url(url) == expected
    session.evidence("non-vpc-before-idle", expected)
    for elapsed in (35, 70):
        time.sleep(35)
        print(json.dumps({"phase": "idle", "elapsed": elapsed}), flush=True)
    assert invoke_url(url) == expected
    session.evidence("non-vpc-after-idle", expected)
    assert not INSTALLED_HELPER.exists()
    assert session.access_intents() == []
    session.evidence("non-vpc-without-helper", {"local_uid": os.geteuid(), "passed": True})
finally:
    for action in (
        session.stop,
        lambda: session.command("destroy", "--yes", "--json", timeout=DESTROY_SECONDS),
    ):
        try:
            action()
        except Exception as error:
            failures.append(error)
    if failures:
        raise ExceptionGroup(f"Plain cleanup incomplete; retain {session.project}", failures)
    assert_application_absent(session.app, session.env, include_lambda_logs=False)
    result = purge_test_metadata(
        session.sdk,
        app=session.app,
        env=session.env,
        bucket=session.bucket,
        account=session.account,
    )
    session.evidence("cleanup", result)
    assert not INSTALLED_HELPER.exists()
    assert not Path("/Library/Application Support/Stelvio/tunnel").exists()
    print(json.dumps(result))
