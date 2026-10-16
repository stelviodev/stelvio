# Traforo end-to-end testing guide

This is the testing-team runbook for changes to `pkg/traforo-client-go` and the
Python VPC tunnel integration. Run the gates in order. A Go test pass alone does
not certify the precompiled wheel asset, installed root helper, or AWS path.

Run commands from the repository root unless a step says otherwise. Commands use
zsh/bash, Python 3.12+, and `uv`. Keep one terminal for the foreground command and
another for observation. Run as an ordinary user; only helper administration and
explicit fault injection should request administrator authorization.

## 1. Select the candidate and reserve a test machine

1. Record `git rev-parse HEAD`, `git branch --show-current`, and `git status --short`.
   Preserve the candidate diff: local Go edits also belong to the tested revision.
2. Use a dedicated macOS 15+ machine with an administrator available. Do not run
   concurrent Stelvio managed sessions, VPN reconfiguration, or other tunnel tests.
   Do not remove someone else's routes or resolver files to make a test pass.
3. Record `sw_vers`, `uname -m`, `go version`, `xcrun --show-sdk-version`, and
   `xcrun clang --version`. Native Intel networking requires an Intel machine;
   Rosetta execution is a separate, narrower check.
4. Keep the lid open and prevent idle sleep during long AWS tests. DocumentDB and
   NAT gateway teardown can take tens of minutes after assertions finish.
5. Confirm the AWS account and region with the testing owner. This guide uses the
   previously exercised `default` profile and `us-east-1`. The live tests create
   billable DocumentDB, NAT, EC2, and associated resources. Bastions persist until
   application teardown, even when the tunnel stops.

Set up an evidence directory outside the source tree:

```bash
export TRAFORO_REPO="$PWD"
export TRAFORO_RUN="$(mktemp -d /private/tmp/traforo-acceptance.XXXXXX)"
uv sync --group dev
export TRAFORO_SOURCE_PYTHON="$TRAFORO_REPO/.venv/bin/python"
export STLV_TEST_AWS_PROFILE=default
export STLV_TEST_AWS_REGION=us-east-1
export AWS_PROFILE="$STLV_TEST_AWS_PROFILE"
export AWS_REGION="$STLV_TEST_AWS_REGION"
export AWS_DEFAULT_REGION="$STLV_TEST_AWS_REGION"
aws sts get-caller-identity > "$TRAFORO_RUN/aws-identity.json"
```

Do not save passwords, decrypted secrets, AWS credentials, SSM tokens, or session
URLs in the evidence bundle. Preserve resource IDs and ownership receipts.

## 2. Run Go tests against the changed source

The canonical module is `pkg/traforo-client-go`; implementation packages are under
`internal/`. Go behavior and Darwin C boundary tests run through Go, not pytest.
The module currently pins Go 1.27.2. Use the pinned compiler rather than silently
upgrading dependencies or changing `go.mod` to fit the machine.

```bash
cd "$TRAFORO_REPO/pkg/traforo-client-go"
go version
go test -mod=readonly -count=1 -timeout=90s ./...
go test -mod=readonly -race -count=1 -timeout=90s ./...
go vet -mod=readonly ./...
go test -mod=readonly ./internal/protocol -run='^$' \
  -fuzz='^FuzzRequest$' -fuzztime=5s -parallel=2
go test -mod=readonly ./internal/helper -run='^$' \
  -fuzz='^FuzzSnapshot$' -fuzztime=5s -parallel=2
cd "$TRAFORO_REPO"
```

Save command output and exit codes. Increase fuzz duration for release testing.
A discovered corpus failure is a failure to investigate, not a reason to remove
the corpus. Preserve independent wire/journal vectors in
`internal/helper/testdata/independent_vectors.json`; do not regenerate expected
bytes from the implementation under test.

These tests cover parsers, capabilities/generations, journal versions/corruption,
identity, descriptor abuse, serialized cleanup/retry/restart, and shutdown. Darwin
syscall scenarios compile their test-only C adapters inside `go test`. They do not
replace a test of actual host routes and DNS.

On an arm64 machine with Rosetta installed, also run:

```bash
cd "$TRAFORO_REPO/pkg/traforo-client-go"
GOARCH=amd64 CGO_ENABLED=1 CC='/usr/bin/clang -arch x86_64' \
  go test -mod=readonly -count=1 -timeout=90s \
  -exec='/usr/bin/arch -x86_64' ./...
cd "$TRAFORO_REPO"
```

Pass: all commands exit zero, without race reports or skipped required scenarios.
Record a Rosetta prerequisite failure as untested, not as native Intel acceptance.

## 3. Rebuild both assets before testing installation

Source changes do not compile automatically during wheel building or installation.
Rebuild the bundled assets explicitly, even for Go test-source changes included in
the fingerprint. The release builder currently requires Go 1.27.2, macOS SDK 15.5,
and `Apple clang version 17.0.0 (clang-1700.0.13.5)`. Select the matching installed
Xcode/SDK environment. If unavailable, use an approved release host; do not bypass
its checks or edit the manifest by hand.

```bash
"$TRAFORO_SOURCE_PYTHON" pkg/traforo-client-go/scripts/build.py
"$TRAFORO_SOURCE_PYTHON" pkg/traforo-client-go/scripts/build.py --check
file stelvio/tunnel/_assets/stelvio-traforo-macos-*
otool -L stelvio/tunnel/_assets/stelvio-traforo-macos-arm64
otool -L stelvio/tunnel/_assets/stelvio-traforo-macos-x86_64
stelvio/tunnel/_assets/stelvio-traforo-macos-arm64 version
/usr/bin/arch -x86_64 stelvio/tunnel/_assets/stelvio-traforo-macos-x86_64 version
cp stelvio/tunnel/_assets/traforo.json "$TRAFORO_RUN/traforo.json"
```

The arm64 version command above assumes an arm64 release host. On Intel, execute
its native asset and record arm64 execution separately. Expected output:
`stelvio-traforo/1 helper=1 carrier=1`.

Pass: both Mach-O architectures, hashes, source inventory/fingerprint, approved
system libraries, minimum macOS 15, and the 16 MiB per-asset bound validate. Record
stripped byte sizes from the manifest. Never test a stale installed helper as if
it were the new Go code.

## 4. Run Python regressions and build an isolated wheel

```bash
"$TRAFORO_SOURCE_PYTHON" -m pytest tests/tunnel tests/test_cli_dev.py \
  tests/test_cli_exit_codes.py tests/aws/test_vpc.py tests/aws/test_document_db.py \
  tests/aws/function/test_function_vpc.py -q
uv build --out-dir "$TRAFORO_RUN/dist"
uv venv "$TRAFORO_RUN/venv"
uv pip install --python "$TRAFORO_RUN/venv/bin/python" \
  "$TRAFORO_RUN"/dist/*.whl 'pymongo>=4,<5'
export STLV_TEST_TUNNEL_PYTHON="$TRAFORO_RUN/venv/bin/python"
export TRAFORO_CLI="$TRAFORO_RUN/venv/bin/stlv"
```

The source Python runs pytest; the separate wheel environment supplies the actual
CLI processes. PyMongo is an explicit dependency of the standalone database proof.
Do not run the wheel checks from the repository root, where Python can import the
source checkout instead of the installed distribution.

```bash
cd "$TRAFORO_RUN"
"$STLV_TEST_TUNNEL_PYTHON" - <<'PY'
import hashlib
import json
import subprocess
import zipfile
import tarfile
from pathlib import Path
import stelvio
from stelvio.tunnel import assets
from unittest.mock import patch

print('Imported package:', stelvio.__file__)
root = Path(stelvio.__file__).parent / 'tunnel' / '_assets'
manifest = json.loads((root / 'traforo.json').read_text())
for arch, entry in manifest['targets'].items():
    data = (root / entry['asset']).read_bytes()
    assert hashlib.sha256(data).hexdigest() == entry['sha256']
    assert len(data) == entry['bytes'] < 16 * 1024 * 1024
    # Execute both on an arm64 Mac with Rosetta. Record prerequisite failures.
    output = subprocess.check_output(
        ['/usr/bin/arch', '-' + arch, str(root / entry['asset']), 'version'], text=True)
    assert output == 'stelvio-traforo/1 helper=1 carrier=1\n'
    print(arch, len(data), output.strip())
for artifact in Path('dist').iterdir():
    if artifact.suffix == '.whl':
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()
            for item in archive.infolist():
                if 'stelvio-traforo-macos-' in item.filename:
                    print('wheel compressed bytes:', item.filename, item.compress_size)
    elif artifact.name.endswith('.tar.gz'):
        with tarfile.open(artifact) as archive:
            names = archive.getnames()
    else:
        continue
    for name in ('stelvio-traforo-macos-arm64', 'stelvio-traforo-macos-x86_64', 'traforo.json'):
        assert any(path.endswith('/' + name) for path in names)
    assert not any(path.endswith('/session-manager-plugin') or '/cmd/check/' in path
                   for path in names)
for system in ('Linux', 'Windows'):
    with patch('platform.system', return_value=system):
        from stelvio.tunnel.session import NetworkSession
        session = NetworkSession(state=None, app='plain', env='test', report=print)
        session.start()
        session.close()
        try:
            with assets.packaged_helper():
                raise AssertionError('Unsupported asset selected')
        except assets.NativeAssetError:
            pass
print('Installed distribution checks PASS')
PY
"$TRAFORO_CLI" tunnel --help
"$TRAFORO_CLI" dev --help
cd "$TRAFORO_REPO"
```

Pass: installed package path points into the wheel venv; both assets are present
in wheel/sdist; the AWS plugin is absent; CLI offers install/start/inspect/cleanup/
reconcile/uninstall, with no recover or network-mode option. Import simulation
checks unsupported-platform guards; it is not Linux or Windows acceptance.

## 5. Capture the host baseline, then install

Before any installation or live session, save:

```bash
ifconfig -l > "$TRAFORO_RUN/interfaces-before.txt"
netstat -rn -f inet > "$TRAFORO_RUN/routes-before.txt"
scutil --dns > "$TRAFORO_RUN/dns-before.txt"
"$TRAFORO_SOURCE_PYTHON" - <<'PY'
import hashlib, json, os, socket
from pathlib import Path
p = Path('/private/etc/resolver')
value = {'interfaces': socket.if_nameindex(), 'resolvers': {
    str(f): hashlib.sha256(f.read_bytes()).hexdigest()
    for f in sorted(p.iterdir()) if f.is_file()
} if p.exists() else {}}
Path(os.environ['TRAFORO_RUN'], 'host-before.json').write_text(json.dumps(value, indent=2))
PY
ls -ld /Library/PrivilegedHelperTools/stelvio-traforo \
  /Library/LaunchDaemons/dev.stelvio.tunnel.plist \
  '/Library/Application Support/Stelvio/tunnel'
```

Missing paths on a fresh machine are expected. If already installed, save
`stlv tunnel inspect` and the installed image digest. Stop if another session is
active or ownership is uncertain. Record whether the baseline was installed;
final cleanup should honor that baseline.

```bash
"$TRAFORO_CLI" tunnel install
"$TRAFORO_CLI" tunnel inspect
shasum -a 256 /Library/PrivilegedHelperTools/stelvio-traforo
/Library/PrivilegedHelperTools/stelvio-traforo version
"$TRAFORO_CLI" tunnel install
```

Approve the macOS authorization request. Expect an empty, certain helper and an
installed digest matching the selected wheel asset. A second install is idempotent.
The pinned private Session Manager plugin is downloaded/verified by installation.
Do not install it manually or add a PATH plugin to compensate for a failure.
Python plugin tests cover corruption, concurrent/interrupted publication, version
changes, and exact-path use. For a fresh-cache test, use a separate test user rather
than deleting another user's cache.

## 6. Native lifecycle acceptance, without AWS

Use a dedicated host. These tests modify real routing/resolver state. Run the
ordinary-user fixture below from the wheel venv, with the file placed in the evidence
directory. It creates two owned routes and resolver scopes but no AWS infrastructure.
The selected CIDRs must not conflict with existing host/VPN routes; a conflict refusal
is expected protection, not permission to delete the existing route.

Save as `$TRAFORO_RUN/hold-host.py`:

```python
import json
import os
import time
from uuid import uuid4
from stelvio.tunnel.helper_client import NativeHelper
from stelvio.tunnel.helper_protocol import ResolverEndpoint

owner = str(uuid4())
lease = NativeHelper().acquire(owner)
try:
    for index, (unit, cidr) in enumerate(
            (('a0000001', '10.254.0.0/16'), ('b0000002', '10.253.0.0/16')), start=1):
        lease.configure(unit=unit, generation=1, vpc_id=f'vpc-{index:08x}',
                        cidrs=(cidr,), resolvers=(ResolverEndpoint(
                            'test-' + unit + '-' + owner.replace('-', '') + '.invalid', 65130),))
    print(json.dumps({'pid': os.getpid(), 'owner': owner}), flush=True)
    while True:
        time.sleep(1)
finally:
    lease.close()
```

The resolver port is deliberately unused; this fixture proves lifecycle, not DNS
answers or TCP. Use the AWS tests below for actual DNS, forwarder and database I/O.
For this lease fixture, its shell is the authenticated command parent. Do not exit
that shell until the test completes.

1. Start `"$STLV_TEST_TUNNEL_PYTHON" "$TRAFORO_RUN/hold-host.py"` in terminal A.
2. In terminal B, inspect: two active units. Save routes, resolver entries and PIDs.
3. Attempt `tunnel uninstall` and `tunnel cleanup`. Both must refuse live ownership;
   the fixture must retain both units. A competing lease must also be refused.
4. Stop terminal A's fixture with Ctrl+C. Poll inspect for at most 60 seconds:
   `owned=False uncertain=False mutating=False`, no units, and owned host effects gone.
5. Repeat, using `kill -KILL <recorded-fixture-pid>` instead of Ctrl+C. The helper
   must clean without a Python cleanup request. Signal only a freshly verified PID.
6. Repeat, restarting the helper while the lease is active:
   `sudo launchctl kill SIGKILL system/dev.stelvio.tunnel`. The service must restart,
   reconcile its journal, and certify empty state. Stop the old fixture afterward.

Additional required native cases (engineering-assisted fault injection):

| Case | Procedure | Pass criterion |
| --- | --- | --- |
| Command death, frozen child | Start a dedicated Python parent that launches `hold-host.py`, recording both PIDs. SIGSTOP the child, SIGKILL only the parent, leaving the child alive. | Helper cleans within 60 seconds while frozen child still exists. Then kill the recorded child. Never use the interactive shell as the disposable parent. |
| Foreign resolver replacement | While fixture owns its unique resolver file, use administrator authorization to move that exact file to a protected backup, then put a different file at its original name. Kill fixture. | Inspect reports uncertainty; cleanup refuses and preserves the foreign file. |
| Cleanup after restored ownership | Restore the exact backed-up original inode, removing only the test-created replacement. Run `tunnel cleanup`. | Empty/certain state; routes/interface/resolver effects removed; helper remains installed. |
| Two-unit isolation | Remove one unit through its lease with a higher generation; keep the other active. | Other unit remains usable; stale generations and wrong capabilities fail. |
| Privilege boundary | Attempt elevated forwarder invocation; attempt a competing nonowner lease and descriptor-bearing control request. | Refused before host changes; existing session remains healthy. Package-local tests cover malformed descriptor variants. |

For resolver replacement, identify the path from the unique owner and inspect the
journal before acting. Back up inode, contents and metadata by moving the file,
not by copying. Use a `finally` cleanup path. Do not erase a foreign file or journal
to force success. This failure scenario requires review of the current ownership
format; it is deliberately not a blanket `sudo rm` command.

The earlier session also used a synthetic DNS responder and TCP handshake over the
revocable datagram carrier to prove two-unit OS DNS and raw utun ownership without
AWS. Its one-off `native.py`, `crash.py` and `upgrade.py` were saved under
`/private/tmp/stlv-v2-proof`, not checked into the repository. They are historical
fixtures, not prerequisites for this runbook; their private APIs must be reviewed
against the candidate before reuse. Do not report the simple hold fixture above
as equivalent to that packet/DNS proof.

## 7. Installation, upgrade and migration acceptance

Run before AWS so a broken installer cannot strand an expensive deployment.
Retain a verified older release image/wheel and its manifest for upgrade tests.
Use the new package's installer for upgrades; never require the old Python package.

1. **Fresh install:** perform section 5 from an absent-helper baseline.
2. **Cross-venv:** create a second venv from the same candidate wheel. Its `tunnel
   inspect` must use the same helper. Sequential sessions from either venv work;
   simultaneous ownership is refused.
3. **Idle upgrade:** install the approved older artifact, verify its digest, then
   run the candidate `tunnel install`. Expect candidate digest, healthy launchd
   service and empty state. Preserve both receipts and command output.
4. **Active upgrade:** with an older helper and live fixture, attempt the new
   install. Expect refusal with unchanged installed digest and healthy live units.
   Stop fixture and retry successfully.
5. **Interrupted replacement:** in a reviewed test fixture using the current
   `installation._bootstrap`, inject exit 73 immediately after the old trusted
   `helper uninstall` succeeds, before publishing the new image. Execute through
   the normal authorization path. Rerun candidate install. It must resume from
   durable replacement evidence and install the candidate without manual deletion.
   Test other replacement cut points when changing transaction ordering.
6. **Legacy migration:** install an approved legacy C helper/receipt using its
   matching legacy installation format, then use only the new installer to migrate.
   Expect the old image retired and current helper healthy. Do not fake legacy
   ownership with arbitrary root files.
7. **Foreign/incompatible evidence:** package tests exercise refusal. Any native
   injection must preserve/restorably isolate the original receipt/journal. Expect
   actionable refusal and retained evidence, with no foreign image overwritten.
8. Run `tunnel cleanup`: installation remains. Run `tunnel uninstall`: helper,
   launchd plist, owned state and replacement artifacts disappear. Private AWS
   plugin stays cached. Reinstall candidate for the AWS lane.

`tests/tunnel/test_install_transaction.py` covers bootstrap cut points and ownership
refusal in a sandbox; it is not a substitute for these native install/launchd tests.

## 8. AWS prerequisites and test collection

Use a disposable account/environment with sufficient VPC, Elastic IP, NAT and
DocumentDB quotas and permissions for deployment, teardown, Secrets Manager,
SSM Session Manager and EC2 Instance Connect. The test uses the account's Stelvio
bootstrap, shared state bucket and AppSync infrastructure. `CliSession` reads
`/stlv/bootstrap` during setup; a new account must first be bootstrapped through
an approved ordinary Stelvio deployment. Do not invent or overwrite that parameter.

```bash
aws ssm get-parameter --name /stlv/bootstrap \
  --query Parameter.Name --output text
aws ec2 describe-availability-zones \
  --query 'AvailabilityZones[?State==`available`].ZoneName' --output text
export STLV_TEST_TUNNEL_AZS=us-east-1a,us-east-1b
"$TRAFORO_SOURCE_PYTHON" -m pytest tests/integration/test_tunnel.py \
  --integration-tunnel -n 0 --collect-only -q
```

Choose two AZs supported by DocumentDB in the selected account/region. The previous
run used those two because the default account AZ choice was unsuitable. The live
fixture currently asserts **macOS 15.7.5** and requires an installed, empty helper.
On another macOS release, arrange a reviewed fixture update and record new platform
acceptance; do not just suppress the assertion and claim the old certification.

Do not combine `--integration-tunnel` with other integration flags or xdist workers.
One helper owns host networking globally. Collection should include `True`, `{}` and
`multi` cases. A skipped test is not AWS acceptance.

## 9. Run the primary real AWS workflow

This is the automated lane executed before compaction, updated for the current Go
source location and a freshly built candidate wheel:

```bash
cd "$TRAFORO_REPO"
caffeinate -i "$TRAFORO_SOURCE_PYTHON" -m pytest \
  'tests/integration/test_tunnel.py::test_example_actual_cli_tls_discovery_reconnect_and_cleanup[True]' \
  --integration-tunnel -n 0 -v --tb=short \
  > "$TRAFORO_RUN/aws-true.log" 2>&1
```

Use another terminal to `tail -f` the log. Do not add a pipe that hides pytest's exit
code. Wait for the final summary and teardown, even after `[100%]` or body success.
The fixture allows up to 20 minutes for startup and one hour for destroy.

The test copies `spikes/vpc-tunnel-app` into a uniquely owned project under
`spikes/dev-vpc-v1/build/p6/<run-id>`. It invokes the installed wheel CLI, and proves:

1. Actual `stlv dev` deploys the Lambda proxy and runs a handler locally, identified
   by a unique marker/process ID rather than merely receiving an HTTP 200.
2. Public Function URL → AppSync invocation bridge → local handler → private
   DocumentDB, using real cluster/member names and port 27017, verified CA/hostname
   TLS, replica discovery, and read/write.
3. Handler reload and 30 seconds idle retain the existing SSM transport.
4. Terminating the exact owned SSM transport causes reconnection and successful
   subsequent handler/database requests, without replacement infrastructure.
5. Normal dev exit leaves the application bastion running, with host state clean.
6. `stlv tunnel start test` connects without bridge or deployment. A separate
   PyMongo client verifies TLS/member discovery/read/write, including reconnect.
7. SIGKILL of standalone triggers helper cleanup; deployed state stays unchanged.
8. Fixture destroy removes application resources, then independently audits AWS
   absence and purges only exact proof-owned metadata/logs/passphrase records.

Check `ownership.json`, `resolved-ownership*.json`, `evidence.jsonl`, `dev*.log`, and
`destroy*.log` in the generated project. Record the app, environment, account,
region, VPC/cluster/bastion IDs, transport receipts, pytest exit code and cleanup
entry. The `cleanup` entry must say `aws_absent: true`. Test-body PASS without that
entry and final successful pytest summary is incomplete.

## 10. Run the remaining AWS matrix

Run sequentially, waiting for each teardown:

```bash
caffeinate -i "$TRAFORO_SOURCE_PYTHON" -m pytest \
  'tests/integration/test_tunnel.py::test_example_actual_cli_tls_discovery_reconnect_and_cleanup[{}]' \
  --integration-tunnel -n 0 -v --tb=short \
  > "$TRAFORO_RUN/aws-config.log" 2>&1
caffeinate -i "$TRAFORO_SOURCE_PYTHON" -m pytest \
  tests/integration/test_tunnel.py::test_multi_vpc_partial_failure_distinct_markers_and_crash_recovery \
  --integration-tunnel -n 0 -v --tb=short \
  > "$TRAFORO_RUN/aws-multi.log" 2>&1
```

The config case tests explicit `{}` persistent policy. The multi case adds distinct
CIDRs/databases and data markers, private-zone DNS, independent VPC failure/admission,
plain/disabled handlers, unavailable configured bastion startup, active uninstall
refusal and dev-command crash cleanup. It creates more billable infrastructure and
needs more quota. Verify the disabled VPC has no access instance, emits a warning,
and does not prevent unrelated invocations. Configured unavailable bastions must
fail/retry connection, never cause replacement infrastructure.

These additional cases were implemented but **not run in the earlier v2 acceptance
session**. Their current results must be recorded separately.

## 11. Optional manual user workflow

Use a separately named disposable copy of `spikes/vpc-tunnel-app`, the candidate
wheel's `stlv`, and a dedicated environment. Record app/environment first to avoid
colliding with someone else's `vpc-tunnel` deployment. Do not let `uv sync` select a
different Stelvio package while claiming to test the candidate wheel.

1. In the disposable app directory, `"$TRAFORO_CLI" deploy test --yes`.
2. `"$TRAFORO_CLI" dev test --yes`; wait for `Network READY` and the bridge connection.
3. In another terminal, `curl --fail-with-body '<printed Function URL>'`.
   Expect `ok`, `read_back`, `host_matches` true and real DocumentDB member names.
   Do not disable TLS verification or replace the hostname with localhost.
4. Ctrl+C and wait for empty/certain inspect. Verify recorded bastion ID still
   running with `aws ec2 describe-instances --instance-ids <bastion-id>`.
5. `"$TRAFORO_CLI" tunnel start test`; it must not deploy or start an invocation
   bridge. Use the same TLS/replica checks as `_standalone_database` in
   `tests/integration/test_tunnel.py`, which reads credentials without printing them.
6. Repeat dev and standalone, killing only the recorded command PID with SIGKILL.
   Poll for empty/certain helper within 60 seconds. Inspect PID birth/start identity
   before signaling; do not use a broad `pkill` on Python, SSH or the AWS plugin.
7. Record no leftover owned transport, route, resolver or interface. A surviving
   orphaned networking child must not keep authority after command death.
8. Destroy this disposable application and independently verify absence as below.

A retained Lambda proxy after dev exit is not a production handler restoration.
Ordinary `stlv deploy` restores production code if retaining an app; acceptance
projects should instead be destroyed when finished.

## 12. Failure handling and independent AWS cleanup

Do not uninstall the helper or remove local ownership files before attempting
recovery and collecting evidence. `tunnel cleanup` handles host state only; it
does not remove EC2, NAT or DocumentDB. There is no `tunnel recover` command.

If pytest fails or is interrupted:

1. Find the exact generated project and read `ownership.json`. Verify the account,
   profile, region and app before any deletion. Preserve all receipts/logs.
2. Stop only verified owned command/transport processes. Poll inspect; if stale
   cleanup persists, run candidate `tunnel cleanup`. Uncertain ownership is a
   failure requiring investigation, never permission to erase journals.
3. From that project, with the same profile/region and candidate CLI, run
   `stlv destroy test --yes --json`. Allow DocumentDB, Lambda ENIs and NAT teardown
   to finish. If it fails, retain state and fix the cause before retrying. Never
   remove backend state to hide resources that still exist.
4. If the multi-VPC run made a private hosted zone, inspect `private-zone.json` and
   use the scoped logic in `CliSession.remove_private_zone` before destroying the
   VPC. Remove only the recorded test records/zone, not a shared hosted zone.
5. Use the independent audit below, then the scoped metadata cleanup helper. It
   refuses a nonempty deployment and verifies ownership/account. It does not
   substitute for application destroy.

From the repository root, set `TRAFORO_PROJECT` to the exact generated project:

```bash
export TRAFORO_PROJECT='/absolute/path/to/spikes/dev-vpc-v1/build/p6/RUN_ID'
"$TRAFORO_SOURCE_PYTHON" - <<'PY'
import hashlib, json, os
from pathlib import Path
import boto3
from tests.integration.assert_tunnel import assert_application_absent, assert_access_absent
from tests.integration.tunnel_cleanup import purge_test_metadata
p = Path(os.environ['TRAFORO_PROJECT'])
o = json.loads((p / 'ownership.json').read_text())
sdk = boto3.Session(profile_name=o['profile'], region_name=o['region'])
assert sdk.client('sts').get_caller_identity()['Account'] == o['account']
sessions, hashes = set(), set()
for receipt in p.glob('resolved-ownership*.json'):
    saved = json.loads(receipt.read_text())
    sessions.update(i['session'] for i in saved['intents'])
    hashes.update(i['owner_hash'] for i in saved['transports'])
hashes.update(hashlib.sha256(s.encode()).hexdigest()[:16] for s in sessions)
assert_application_absent(o['app'], o['env'], include_lambda_logs=False, sdk=sdk)
assert_access_absent(sdk, sessions, hashes)
result = purge_test_metadata(sdk, app=o['app'], env=o['env'],
                             bucket=o['bucket'], account=o['account'])
print(json.dumps(result, indent=2))
(p / 'independent-cleanup.json').write_text(json.dumps(result, indent=2))
PY
```

Run this after successful tests too as an independent audit; metadata cleanup is
idempotent, so removal counts may be zero. Confirm known VPC/cluster IDs separately
in AWS. Expected: no proof networking/database/Lambda/IAM/SSM resources, no active
recorded transport, no exact proof backend versions/logs/passphrase. Terminated
EC2 or deleted NAT descriptions may remain visible; those terminal states are
accepted by the audit. Retain the existing shared state bucket and AppSync API.
Do not run a broad account-wide cleanup script without its own scope review.

## 13. Restore and certify the host baseline

```bash
"$TRAFORO_CLI" tunnel inspect
"$TRAFORO_CLI" tunnel cleanup
"$TRAFORO_CLI" tunnel inspect
```

Pass: no ownership, uncertainty, mutation or units; helper still installed.
If baseline had no helper, finish with:

```bash
"$TRAFORO_CLI" tunnel uninstall
ifconfig -l > "$TRAFORO_RUN/interfaces-after.txt"
netstat -rn -f inet > "$TRAFORO_RUN/routes-after.txt"
scutil --dns > "$TRAFORO_RUN/dns-after.txt"
```

Repeat section 5's Python baseline capture to `host-after.json`. Compare exact
interface inventory and resolver filename/content digests. Inspect route and DNS
diffs for owned changes. Pre-existing Apple/VPN utun interfaces are normal; the
criterion is restoration of the baseline, not blanket removal of every utun.
Explain unrelated DHCP/VPN changes rather than silently accepting a large diff.

Verify the following absent after fresh-install/uninstall acceptance:

- `/Library/PrivilegedHelperTools/stelvio-traforo`
- legacy `/Library/PrivilegedHelperTools/dev.stelvio.tunnel`
- `/Library/LaunchDaemons/dev.stelvio.tunnel.plist`
- `/Library/Application Support/Stelvio/tunnel`
- replacement `.stelvio-traforo.*` staging/retired artifacts in the helper directory

The private Session Manager plugin cache remains intentionally. If the machine had
an existing helper baseline, keep the approved installation and certify empty host
state instead; arrange restoration of its approved version with the machine owner.

## 14. Report results without overstating acceptance

Attach candidate commit/diff, manifest, architecture/OS/toolchain, all command exit
codes, wheel identity, installed digest, native case results, project ownership,
AWS evidence/audits and before/after host snapshots. Mark each case pass/fail/not
run. Append new evidence to `tasks/dev-vpc.md`; update walkthrough provenance when
source excerpts change. Do not rewrite historical results as candidate acceptance.

Historical reference, 2026-10-16, before subsequent Go edits/layout changes:

| Gate | Recorded result |
| --- | --- |
| Python affected regressions | 510 passed |
| Go race + bounded fuzz | Passed; request 830,446 executions, snapshot 449,232 |
| Both assets / Rosetta package tests / installed wheel | Passed |
| Native arm64 macOS 15.7.5 | Two-unit TCP/DNS, ownership/descriptor checks, idle/interrupted/legacy upgrades, frozen orphan, helper restart, failed-cleanup recovery passed |
| Default-profile AWS single-VPC `True` | Passed in 1541.03 seconds including teardown; dev/standalone TLS/member/read-write/reconnect and standalone SIGKILL |
| AWS cleanup | Application absent; 61 proof backend versions, one Lambda log group and passphrase removed; shared infrastructure retained |
| Final host | Public uninstall passed; interfaces/resolver digests restored, no remaining owned IPv4 routes |
| Extra config/multi-VPC AWS and separate full dev SIGKILL | Not certified in that run |
| Real Intel networking / other macOS releases | Pending |

Historical evidence was stored under `/private/tmp/stlv-v2-proof` and project
`spikes/dev-vpc-v1/build/p6/7f123f`. Temporary files can disappear and are not durable
team dependencies. Linux/WSL/Windows backends and CI/CD remain outside this test
scope. A new Go change requires new source-coherent assets and relevant fresh
acceptance, regardless of those earlier passes.
