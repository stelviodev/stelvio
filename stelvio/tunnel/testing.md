# Traforo end-to-end testing guide

This is the testing-team runbook for Stelvio’s pinned Traforo release and the
Python VPC tunnel integration. Run the gates in order. A Go test pass alone does
not certify the downloaded release asset, installed root helper, or AWS path.

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
3. Record `sw_vers`, `uname -m`, the pinned Traforo release version and
   the Python/Stelvio versions. Native Intel networking requires an Intel machine;
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

## 2. Verify the upstream release and testing boundary

Traforo source and CI builds live in https://github.com/stelviodev/traforo.
`pkg/traforo-client-go` is a reference snapshot only: do not build it, use it to
certify release bytes, or include it in Python distributions. Go unit/race/fuzz
and Darwin boundary testing belong to the upstream release workflow. Record that
release's CI evidence; Stelvio acceptance validates the downloaded executable and
its Python/host/AWS interoperability. Neither Go nor Xcode is needed to install.

The current release pin is **v0.0.4**:

| Architecture | Download | SHA-256 |
| --- | --- | --- |
| arm64 | `https://github.com/stelviodev/traforo/releases/download/v0.0.4/traforo-darwin-arm64` | `96bebda932a84d2ec277ad60ada6f61db2c1525fc65247b3d17239f4ef696fcb` |
| x86_64 (Go amd64) | `https://github.com/stelviodev/traforo/releases/download/v0.0.4/traforo-darwin-amd64` | `dd0c8f07bfceb2dd5267cd7abfbd0762dbc6d5a905fb76b7e4d89fdd227895d2` |

## 3. Download and verify both release architectures

During private-repository development, explicitly set the testing-shell token:

```bash
export GITHUB_TOKEN="$(gh auth token)"
```

Do not print it or enable shell tracing. Production code uses only the token
already in the environment; it never executes `gh`. Without a token the downloader
uses the public release URL. With one it uses the pinned GitHub release-asset API,
removing authorization before cross-origin redirects. A private unauthenticated
request should fail with an actionable error; the same path should work after the
repository is public. Test those HTTP branches with mocked download tests now.

On arm64 with Rosetta available, run:

```bash
"$TRAFORO_SOURCE_PYTHON" - <<'PYCODE'
import hashlib, json, os, subprocess
from pathlib import Path
from unittest.mock import patch
from stelvio.tunnel import assets
results = []
for architecture in ('arm64', 'x86_64'):
    with patch.object(assets, '_architecture', return_value=architecture):
        image = assets.ensure_traforo()
        digest = hashlib.sha256(image.read_bytes()).hexdigest()
        assert digest == assets.expected_digest()
        assert subprocess.check_output(
            ['/usr/bin/arch', '-' + architecture, str(image), 'version'], text=True
        ) == assets.EXPECTED_VERSION
        print(subprocess.check_output(['/usr/bin/otool', '-L', str(image)], text=True))
        results.append(dict(architecture=architecture, path=str(image),
                            sha256=digest, bytes=image.stat().st_size))
Path(os.environ['TRAFORO_RUN'], 'release-images.json').write_text(json.dumps(results, indent=2))
PYCODE
export STLV_TEST_TRAFORO_IMAGE="$("$TRAFORO_SOURCE_PYTHON" -c \
  'from stelvio.tunnel.assets import ensure_traforo; print(ensure_traforo())')"
```

On Intel, record arm64 execution as pending. Both images must match their pins,
Mach-O architecture and protocol output, stay within 16 MiB, and link only approved
system libraries. Cached images must be owned regular executable files, not symlinks.
Re-running should use the verified cache without a download. A missing/corrupt cache
is repaired before atomic publication. Ordinary unit tests stay offline; enabling
`STLV_TEST_TRAFORO_IMAGE` executes real process tests using the pre-downloaded image.

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
import json, zipfile, tarfile
from pathlib import Path
from unittest.mock import patch
import stelvio
from stelvio.tunnel import assets
from stelvio.tunnel.session import NetworkSession
print('Imported package:', stelvio.__file__)
assert '/venv/' in str(stelvio.__file__)
for artifact in Path('dist').iterdir():
    if artifact.suffix == '.whl':
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()
    elif artifact.name.endswith('.tar.gz'):
        with tarfile.open(artifact) as archive:
            names = archive.getnames()
    else:
        continue
    assert not any('traforo-client-go/' in path or '/_assets/' in path
                   or path.endswith('/session-manager-plugin') for path in names)
    assert not any('stelvio-traforo-macos-' in path or 'traforo-darwin-' in path for path in names)
for system in ('Linux', 'Windows'):
    with patch('platform.system', return_value=system):
        session = NetworkSession(state=None, app='plain', env='test', report=print)
        session.start()
        session.close()
        try:
            assets.ensure_traforo()
            raise AssertionError('Unsupported asset selected')
        except assets.NativeAssetError:
            pass
print('Installed distribution checks PASS')
PY
"$TRAFORO_CLI" tunnel --help
"$TRAFORO_CLI" dev --help
cd "$TRAFORO_REPO"
```

Pass: installed package path points into the wheel venv; no native executable or
reference Go source is present in wheel/sdist; the AWS plugin is absent; CLI offers install/start/inspect/cleanup/
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
installed digest matching the selected release pin. A second install is idempotent.
The pinned private Session Manager plugin is downloaded/verified by installation.
Do not install it manually or add a PATH plugin to compensate for a failure.
Python plugin tests cover corruption, concurrent/interrupted publication, version
changes, and exact-path use. For a fresh-cache test, use a separate test user rather
than deleting another user's cache.

## 6. Native lifecycle acceptance, without AWS

Use a dedicated host. These tests modify real routing/resolver state. Run the
ordinary-user fixture below from the wheel venv, with the file placed in the evidence
directory. It creates two owned VPC units with routes and resolver scopes but no AWS infrastructure.
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
Retain a verified older release image/wheel and its release digest for upgrade tests.
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
downloaded release and a freshly built candidate wheel:

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

The private Traforo and Session Manager plugin caches remain intentionally.
Test ordinary uninstall with network access unavailable: the trusted installed
image is sufficient. Forwarder startup also uses the installed pin without a cache
or download. Interrupted replacement with no installed image may need the cached
release or network access to resume. If the machine had
an existing helper baseline, keep the approved installation and certify empty host
state instead; arrange restoration of its approved version with the machine owner.

## 14. Report results without overstating acceptance

Attach candidate commit/diff, release pins, architecture/OS/toolchain, all command exit
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
scope. A new Go change requires new pinned release artifacts and relevant fresh
acceptance, regardless of those earlier passes.


### External release acceptance, 2026-10-17

The v0.0.4 distribution migration was tested using this revised runbook: both real
release hashes, native/Rosetta version and library inspection, 455 Python regressions
with explicit image interop, clean-wheel inventories/platform guards, native two-unit
routing/DNS/TCP, owner death/restart/failed-cleanup recovery, idle/interrupted/legacy
upgrade, fresh install, cross-venv access and offline uninstall passed.

Default-profile/us-east-1 app `stlv-657643/test` passed the primary `[True]` AWS lane
in 1541.12 seconds including teardown. Dev and standalone verified TLS, member
discovery, read/write and reconnection; standalone SIGKILL cleaned automatically.
Normal exit retained the application bastion; teardown removed it. Fixture and
independent audits confirmed AWS absence, removing 62 backend versions, one log
group and passphrase. The original installed helper and exact interface/resolver
baseline were restored. Route differences were LAN ARP expiry timers only.
Evidence is under `/private/tmp/traforo-release-proof` and the generated project.

The `{}` and multi-VPC AWS lanes, separate full dev SIGKILL, real Intel networking
and other macOS releases were not certified by this run. These results apply to the
pinned release, not future releases or changes to the reference snapshot.
