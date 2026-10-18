# Traforo end-to-end testing guide

This is the testing-team runbook for Stelvio’s pinned Traforo release and the
Python VPC tunnel integration. Run the gates in order. A Go test pass alone does
not certify the downloaded release asset, installed root helper, or AWS path.

Run commands from the repository root unless a step says otherwise. Commands use
zsh/bash, Python 3.12+, and `uv`. Keep one terminal for the foreground command and
another for observation. Run as an ordinary user; only helper administration and
explicit fault injection should request administrator authorization.

## Current release and version gate

Managed networking requires the pinned Traforo base version, currently v0.0.5.
New builds report one semantic version, with no component capability command.
The existing v0.0.5 release has the old output and is accepted only when its bytes
match the pinned architecture digest. Leave `STLV_TRAFORO_DEV_BINARY` unset for release acceptance. The optional
**Developer-image route** tests local Go changes. Both routes validate the binary version
before helper installation or host changes; no Python DNS fallback exists.
Unsetting the override restores normal pinned-release networking when installation
can safely replace the developer image.

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

The monorepo source under `pkg/traforo-client-go` is authoritative for the Go client
API and unit tests. The repository split mirrors it to
https://github.com/stelviodev/traforo, which owns release builds and Go implementation
CI. Normal Stelvio installs still use pinned releases, not automatic compilation.
Run local Go tests directly:

```bash
cd "$TRAFORO_REPO/pkg/traforo-client-go"
go test -mod=readonly -count=1 -timeout=90s ./...
go test -mod=readonly -race -count=1 -timeout=90s ./...
go test -mod=readonly ./internal/protocol -run='^$' -fuzz='^FuzzRequest$' -fuzztime=5s -parallel=2
go test -mod=readonly ./internal/dnsrelay -run='^$' -fuzz='^FuzzAnswer$' -fuzztime=5s -parallel=2
go test -mod=readonly ./internal/forwarder -run='^$' -fuzz='^FuzzControl$' -fuzztime=5s -parallel=2
go test -mod=readonly ./internal/helper -run='^$' -fuzz='^FuzzSnapshot$' -fuzztime=5s -parallel=2
cd "$TRAFORO_REPO"
```

Go and the macOS SDK are required for local source tests/builds; neither is needed
for ordinary release installation. Record upstream CI evidence when certifying a
release. Local source tests alone do not certify downloaded bytes.

The current release pin is **v0.0.5**:

| Architecture | Download | SHA-256 |
| --- | --- | --- |
| arm64 | `https://github.com/stelviodev/traforo/releases/download/v0.0.5/traforo-darwin-arm64` | `b408afcaca01f760f4999c674a21271302c98eedad61678067ab80f844a4ba0f` |
| x86_64 (Go amd64) | `https://github.com/stelviodev/traforo/releases/download/v0.0.5/traforo-darwin-amd64` | `4008c435efb5bb3913784a10da61df8fdbaadcbc324e42f2bfd470dbbc86faba` |

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
        ) in (f'v{assets.TRAFORO_VERSION}\n', assets.LEGACY_VERSION.decode())
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

### Developer-image route

To test local Go changes without publishing a release, use this route instead of
selecting a downloaded image. Run the Go checks above, then:

```bash
uv run python pkg/traforo-client-go/scripts/build.py
# Run the exact shell-quoted export printed by the script in every testing shell:
export STLV_TRAFORO_DEV_BINARY='/absolute/path/printed/by/the/script'
export STLV_TEST_TRAFORO_IMAGE="$STLV_TRAFORO_DEV_BINARY"
```

Install GoReleaser v2.18.3, matching the workflow. The script uses `.goreleaser.yml`
for both macOS architectures and the installed SDK. It validates Mach-O architecture,
size, linked libraries, and version; Apple Silicon requires Rosetta for amd64.
On Intel, arm64 is structurally checked with embedded-version/GoReleaser metadata
validation, and native execution is explicitly pending. Artifact names match CI:
`traforo-darwin-arm64` and `traforo-darwin-amd64`. Both images and `build.json`
are atomically published together in a unique ignored `build/traforo-*/` bundle.
The host image is installed through normal macOS authorization. Existing sessions must stop before changing images. No release pins,
download caches or persistent root markers are changed. The export is mandatory:
the script's environment affects only its own installation, not its parent shell.
Verify `"$STLV_TRAFORO_DEV_BINARY" version` reports
`v0.0.5-dev+g<12-character-commit>`. This base version must match Stelvio's pin.
Provenance records the full commit and source/config fingerprint, distinguishing
uncommitted edits. Internal wire/journal format identifiers remain unchanged.

For nonprivileged validation, use `make -C pkg/traforo-client-go build` or add
`--build-only` to the script. It runs the same snapshot release pipeline with
publication disabled and no GitHub token. Build failure preserves prior bundles.
Matching local/CI bytes additionally requires matching source, version, commit,
Go toolchain and SDK; a snapshot intentionally has a different version from a tag.

Run the Python tests below with both variables to include real local-image process
interop; pytest never compiles Go. Use this checkout's editable Stelvio environment
for developer-mode acceptance. Wheel packaging/administration acceptance remains a separate path; managed
networking from the wheel must receive the same developer override to select this snapshot. The same local binary must be selected for
helper installation and the isolated forwarder, which executes the root-owned image.

Native acceptance must cover local install/reuse, two-unit routing/DNS/TCP, actual
forwarder IPC, active replacement refusal, interrupted local replacement recovery,
and restoration after clearing the variable. Verify a real foreign-write ACL is
refused; unit mocks alone do not certify Darwin ACL behavior. Test missing/relative
paths, symlinks, hard links, wrong ownership/modes, architecture/protocol mismatch,
and mutation during validation. Invalid overrides never fall back.

To return to release behavior:

```bash
unset STLV_TRAFORO_DEV_BINARY
uv run stlv tunnel install
```

Without the variable, managed startup uses the compatible v0.0.5 pin.
`tunnel install` restores that pinned image when safe. Inspect
and ordinary uninstall work even with an invalid override or deleted build file.
Older Stelvio versions do not support this escape hatch. Rebuild after source edits;
the selector deliberately does not compile or enforce live source fingerprints.
For a release run, also reset `STLV_TEST_TRAFORO_IMAGE` to its downloaded image path.

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
  --integration-macos-privileged --integration-tunnel -n 0 --collect-only -q
```

Choose two AZs supported by DocumentDB in the selected account/region. The previous
run used those two because the default account AZ choice was unsuitable. The fixture accepts macOS 15+ and preflights a certain, empty host. It preserves
and restores the installed helper. Record the actual OS/architecture tested; support
checks do not certify untested platforms.

The AWS lane requires both `--integration-macos-privileged` and `--integration-tunnel`.
Do not combine them with other tier flags or xdist workers.
One helper owns host networking globally. Collection should include `True`, `{}` and
`multi` cases. A skipped test is not AWS acceptance.

## 9. Run the primary real AWS workflow

This is the automated lane executed before compaction, updated for the current Go
downloaded release and a freshly built candidate wheel:

```bash
cd "$TRAFORO_REPO"
caffeinate -i "$TRAFORO_SOURCE_PYTHON" -m pytest \
  'tests/integration/test_tunnel.py::test_example_actual_cli_tls_discovery_reconnect_and_cleanup[True]' \
  --integration-macos-privileged --integration-tunnel -n 0 -v --tb=short \
  > "$TRAFORO_RUN/aws-true.log" 2>&1
```

Use another terminal to `tail -f` the log. Do not add a pipe that hides pytest's exit
code. Wait for the final summary and teardown, even after `[100%]` or body success.
The fixture allows up to 20 minutes for startup and one hour for destroy.

The test copies `tests/integration/fixtures/tunnel_app` into a uniquely owned
pytest temporary directory. It invokes the installed wheel CLI, and proves:

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
  --integration-macos-privileged --integration-tunnel -n 0 -v --tb=short \
  > "$TRAFORO_RUN/aws-config.log" 2>&1
caffeinate -i "$TRAFORO_SOURCE_PYTHON" -m pytest \
  tests/integration/test_tunnel.py::test_multi_vpc_partial_failure_distinct_markers_and_crash_recovery \
  --integration-macos-privileged --integration-tunnel -n 0 -v --tb=short \
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
export TRAFORO_PROJECT='/absolute/path/to/retained/pytest/application'
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
pinned release, not future releases or edits to the local Go source.

### Local developer build acceptance, 2026-10-17

The environment-opt-in route passed Go unit/race tests and bounded request/snapshot
fuzzing, plus 485 affected Python tests with real local-image interop. Native arm64
macOS15.7.5 proved build/install, local image reuse, two-unit OS DNS/TCP/carrier,
active changed-image refusal, pin restoration after clearing the override, interrupted
local replacement recovery, real ACL refusal, and inspect/offline uninstall with a
missing developer file. Wheel/sdist exclusion, docs and source provenance checks pass.

The original idle helper and owned host baseline were restored. Exact resolver
inventory/digests and original interfaces match; new utun5/utun6 were independently
attributed to Apple's identityservicesd using root-visible socket ownership and left
intact. Record that global interface difference, rather than removing foreign devices
or claiming exact equality. IPv4 route differences were ordinary ARP expiry timers.
Evidence: `/private/tmp/traforo-local-proof`. No new AWS run, Intel or other macOS
acceptance is claimed by these developer-mode tests.

## DNS migration acceptance sequence

1. Run Go unit/race tests and DNS/control fuzzing above. They cover DNS semantics,
   stale generations, direct-host payload verification, and socket/worker bounds.
2. For release acceptance, unset `STLV_TRAFORO_DEV_BINARY` and set
   `STLV_TEST_TRAFORO_IMAGE` to the verified cached v0.0.5 executable. Alternatively,
   build/install local source, export the printed override and set
   `STLV_TEST_TRAFORO_IMAGE="$STLV_TRAFORO_DEV_BINARY"`. Run `tests/tunnel` to include
   independent fixed-wire UDP/TCP and actual isolated-runtime interoperability.
3. Run actual macOS tests, without AWS deployments:

   ```bash
   "$TRAFORO_SOURCE_PYTHON" -m pytest tests/integration/test_tunnel_dns.py \
     --integration-macos-privileged --integration-tunnel -n 0 -v --tb=short
   ```

   Require public-fallback rejection with an observed scoped outage query,
   two-VPC DNS/host-TCP payloads, direct and SOCKS readiness, CNAMEs, independent
   failure and port retention across reconnect. Inspect must be empty/certain.
4. Run crash/restart and interrupted-install gates, then the primary AWS workflow
   in section 9 using a candidate wheel and the same image selection in its CLI process.
   Keep the local image unchanged while sessions are active. Verify standalone
   access, real TLS/member discovery/read-write, transport recovery, and teardown.
5. If testing a developer build, unset the override and restore v0.0.5 through
   `stlv tunnel install` when idle. Managed startup must use the release afterward.
   Inspect/cleanup/uninstall work even when the developer build is absent.
6. Audit owned host state and AWS absence. Record new results below separately
   from the historical Python-relay and pinned-release evidence.

Stelvio itself no longer requires `dnspython`. Application drivers such as PyMongo
may independently install it for their own DNS/SRV support; that is separate from
Traforo and the tunnel. A clean Stelvio-only wheel environment should have no
`dns` module.

## 2026-10-17 Go DNS migration acceptance

These results apply to the Go DNS migration, separately from historical Python
relay/pinned-release evidence above. Source and test reviews completed; reported
boundedness, retirement fencing, direct-path coverage, and deadline assertion gaps
were fixed and rechecked.

| Gate | Observed result |
| --- | --- |
| Python regressions | 485 passed, including actual DNS/control/runtime interop; final boundary subset 73 passed |
| Go | Full unit/race suite passed; DNS fuzz 276,509 and control fuzz 86,885 executions with 5-second budgets |
| Native arm64 | Two macOS DNS tests passed: scoped CNAME resolution, observed outage/public-fallback rejection, two-VPC real utun/carrier TCP payloads, independent failure and listener-port retention |
| Crash/recovery | Parent death with frozen child, runtime death, helper restart, retained foreign-cleanup refusal and recovery passed |
| Administration | Active changed-image refusal, idle replacement, interrupted replacement recovery, missing-override inspect and offline uninstall passed |
| Packaging | Wheel/sdist contain no Go module, developer scripts or executables; fresh Stelvio-only wheel imports without dnspython; non-VPC startup and unsupported-platform guards passed |
| x86_64 | Build, version/capabilities and DNS/control Go tests through Rosetta passed; native Intel networking pending |
| AWS default/us-east-1 | `stlv-b37960/test`: public Function URL → local handler → verified DocumentDB TLS/member discovery/read-write; reload/idle and transport recovery passed; standalone read-write/recovery and SIGKILL cleanup passed |
| AWS final result | 1 passed in 1389.96s, including teardown; 49 resources deleted; independent absence audit passed; 59 metadata versions and 1 log group removed, passphrase absent |
| Host final | Original helper image restored, exact starting interfaces/resolver files restored, empty/certain inspection, no replacement artifacts |

AWS-tested arm64 image SHA-256: `456899ac5e980fd40e35243f17498b02ee9bfcbf93052f10167f12277920eba0` (7,690,098 bytes).
Final Go source fingerprint: `152d5de5b4a049885f2ee77ecdfb79d794a14536adac07d7f74e9b2316b85b47`. Go 1.27.2, macOS SDK 15.5,
Clang 17, minimum macOS 15; only approved Apple system libraries. The DNS dependency
adds Security.framework through Go TLS/X.509 linkage. x86_64 is 8,159,336 bytes;
both remain below 16 MiB. No executable ships in a Python distribution.

The AWS run used an installed candidate wheel whose production Python files were
checked byte-for-byte against the current source. Acceptance evidence is retained
locally under `/private/tmp/traforo-dns-proof`; testing teams should create their
own evidence directory and follow the steps above rather than depend on that path.

At the time of this historical local-build acceptance, pins remained v0.0.4,
which lacked DNS capabilities. Managed networking required the local build/export
until a compatible external release was published and pinned. Release pinning,
native Intel acceptance and other platforms were follow-ups at that point;
v0.0.5 pin validation is recorded separately below. No commits, branch changes,
or CI/CD changes were made during that migration.

## 2026-10-17 v0.0.5 release-pin validation

This pin update makes the developer override optional again. Both actual release
assets were downloaded through the production downloader, with authentication
supplied only through the test process environment. Their hashes match the table
above. Mach-O architecture, approved system-library dependencies, base protocol
output, `forwarder=2` / `dns=1` capabilities and verified cache reuse passed.

| Release image | Bytes | Execution |
| --- | ---: | --- |
| arm64 | 7,772,690 | Native macOS arm64 |
| x86_64 | 8,237,176 | Rosetta on arm64 |

334 affected Python tests passed, including actual DNS and isolated-runtime
interoperability with the downloaded arm64 release, with no developer override.
The documentation build, walkthrough JavaScript and embedded-source checks passed.
Source and test reviews reported no material findings.

No privileged helper installation, live host networking proof or AWS deployment
was performed for this update. The installed helper digest remained
`fd65dee8e07f72f0303a51140a7b1158dcfa11f5cc6856ebbb675a21c1516625`.
Previous local-build AWS acceptance remains historical and does not establish
release-image AWS acceptance. Real Intel networking remains pending.
Local evidence: `/private/tmp/traforo-v005-proof`.

Wheel and sdist exclusion checks passed: neither contains Go source, developer
build scripts or executable assets; wheel metadata has no dnspython requirement.


## 2026-10-18 unified GoReleaser and semantic-version acceptance

This acceptance is separate from the historical local-build AWS and v0.0.5 release
proofs above. No helper installation, host route/DNS mutation or AWS deployment was
performed. The installed helper remained at SHA-256
`fd65dee8e07f72f0303a51140a7b1158dcfa11f5cc6856ebbb675a21c1516625`.

- The official GoReleaser v2.18.3 Darwin arm64 tool was downloaded to
  `/private/tmp/traforo-goreleaser-proof`; its archive matched GitHub's published
  SHA-256 `821c58ab8c31d57bb331cab9f1a703eb3673bff4fce0db9cc3c4a038af14f76f`.
- `make -C pkg/traforo-client-go build` exercised the real snapshot release pipeline,
  publication disabled. Both architecture outputs passed Mach-O, size, library,
  version and GoReleaser metadata checks. Arm64 executed natively; amd64 through
  Rosetta. Both report `v0.0.5-dev+g671d8150fe20`.
- Final bundle: `pkg/traforo-client-go/build/traforo-g7lj3c0p`. Source fingerprint:
  `9f6555cc8fb3856c2ef03a14ad00cb10558cb11ef6841fe9ebf9ae43b32389ac`. Full commit: `671d8150fe20047801f810e1e17cebc3e9dbc010`.
- arm64: 7,690,130 bytes, SHA-256
  `602803d9e22acd51ce54341efc1530fd9fd9ce5836c64a546e38ffe94851f198`.
- amd64: 8,159,352 bytes, SHA-256
  `10bd6ab4f6feedaa6c3c44c75c017bd2e710082c30772c90a10809311cffc2a4`.
- `make lint vet test race` passed with zero lint findings. The affected Python/CLI
  suite passed **350 tests** with both developer/testing environment variables set,
  including actual DNS and isolated-runtime process interoperability. No pytest
  test compiled Go. Build failures, metadata/config/source changes, missing Rosetta,
  version mismatches, and exact installed-image snapshot selection are covered.
- Both previously downloaded pinned v0.0.5 images passed the new digest-bound legacy
  version validation. Release pins, URLs/checksums and caches were not changed.
- Documentation build and Python wheel/sdist builds passed. Both distributions
  exclude Go source, developer build scripts, release configuration and executables.

Native Intel networking remains pending. Intel's inability to execute arm64 is
covered by a simulated builder test, not a claim of native Intel acceptance.


## 2026-10-18: maintained spike replacement tests

The authoritative coverage map is [spike-coverage.md](spike-coverage.md).
Historical scripts remain unchanged apart from warnings; do not use them as current
installer/recovery instructions. Maintained tests use checked-in fixtures and pytest
temporary evidence directories, with no dependence on spike scripts.

Run the no-AWS native lane separately:

```bash
export STLV_TEST_STELVIO_WHEEL='/absolute/path/to/candidate.whl'
uv run pytest tests/integration --integration-macos-privileged -n 0 -v --tb=short
```

Use both macOS and tunnel flags for AWS acceptance. `run_all.sh` never runs either
host lane, including when `STLV_TEST_TUNNEL=1`. All host tests serialize across users
and restore the original installed image or absent-helper baseline. An uncertain
journal or foreign resource fails acceptance and preserves evidence.

Run these lanes in an attended, unlocked macOS desktop session. Installation,
replacement, uninstall and root-owned fault fixtures use normal administrator
authorization dialogs, including when the expected outcome is active-session
refusal. Authorization expires during long AWS deployments; respond to later
dialogs as well. A 180-second authorization timeout fails the test and can prevent
final helper restoration. Retain the saved original image and inventories, restore
through the ordinary installer transaction, and audit the baseline before retrying.
Do not bypass authorization or delete journals to finish a run.

Pytest can print a test's `PASSED` line before its fixture teardown finishes.
Wait for the final suite summary, each application's `cleanup` evidence record,
and the final host inventory. AWS may retain Lambda VPC interfaces while it
retires them after function deletion; leave the provider's bounded teardown
running. An empty DocumentDB list alone does not certify VPC or IAM cleanup.
See [AWS's Hyperplane ENI lifecycle guidance](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html).

The 100-cycle test runs all 100 cycles. Legacy migration and cross-venv acceptance
need their explicit artifacts; missing artifacts are named skips and remain pending.
Use a new `--basetemp` path for every run. Do not erase failed-run evidence before
certifying host/AWS restoration. Results below are current-run evidence, separate
from all earlier historical records.


### 2026-10-18 regression migration: current execution evidence

- Python tunnel and CLI regressions: **358 passed, 5 image-dependent skips**.
- Go lint/vet/unit tests passed. Full amd64/Rosetta race suite passed.
  Native ARM64 race is **blocked**, not passed: pinned gVisor
  `pkg/sync/race_arm64.s` loads the CAS new value into the old-value register.
  The stream regression now fails early on that combination instead of hanging.
  Use `GOARCH=amd64 CGO_ENABLED=1 make race` with Rosetta. No dependency pin changed.
- Bounded fuzzing passed: DNS 110,705 executions; helper requests 486,772;
  snapshots 253,351; forwarder control 35,947 (three-second budgets, two workers).
- Native final run: **12 passed in 197.74 seconds** on macOS 15.7.5 arm64,
  including all 100 cycles, owner/parent death, restart, descriptor/elevation
  refusal, foreign resolver protection/restoration, cross-venv, interrupted
  replacement and checksum-verified legacy migration. No skips.
  Evidence: `/private/tmp/stelvio-native-regression-final-20261018`,
  log `/private/tmp/stelvio-native-regressions-final.log`. Original installed
  image `fd65dee8e07f72f0303a51140a7b1158dcfa11f5cc6856ebbb675a21c1516625`
  was restored with independent baseline checks.
- The endurance run exposed an ephemeral UDP/TCP listener-port collision. Go now
  retries only address-in-use collisions, at most eight attempts, closing every
  failed UDP socket. Independent injected tests cover retry, exhaustion and
  unrelated errors. Pinned external releases remain unchanged; this fix is tested
  through `STLV_TRAFORO_DEV_BINARY` until a future release carries it.
- AWS execution build: `pkg/traforo-client-go/build/traforo-6pntvbfo/build.json`,
  version `v0.0.5-dev+g395a5cc3df2c`, source fingerprint
  `d1b0c40ae3bbc14047e20391f6b8fe07fdc7734057edf725456901e46dd6656d`.
  Both architectures built and validated (amd64 through Rosetta). A subsequent
  reviewed-source build, `build/traforo-29lz0w9r/build.json`, has source fingerprint
  `47f2a29a02d1d49dd59fd0a42a4adb78812f892f6c56b16433e73efeed856453`.
  Both executable hashes are identical between builds; the changes were test-only.
  Arm64 SHA-256: `9a300bdb5acde28e6fc3bf7ec95e9e70ee337d6b9e463ddd93a6ab0e511713c6`;
  amd64: `d1e550f71be8a7edcfb6224ac9f9f7baf677b1f844d562ee675222adeb8969ed`.
- Wheel/sdist built under `/private/tmp/stelvio-tunnel-regressions/dist`;
  neither contains Go source or Traforo executables.
- All four AWS scenarios passed across the original and retry runs using the
  default profile in us-east-1: both explicit bastion forms, multi-VPC faults and
  real non-VPC CLI invocation with no installed helper. This includes verified
  DocumentDB TLS/member discovery/read-write, standalone access/reconnection,
  SSH host-key-change rejection/restoration, private aliases, independent outages,
  invocation admission, partial startup and command-death cleanup. Normal session
  exit retained deployed bastions; application teardown subsequently removed them.
- The original AWS runner finished **2 passed, 2 failed, 1 teardown error** in
  4773.09 seconds. Failures were 180-second macOS authorization timeouts, not
  networking assertions. Its three deployed applications were destroyed, and the
  original helper was restored through the normal authorization transaction.
  Preserve `/private/tmp/stelvio-aws-regression-20261018` and
  `/private/tmp/stelvio-aws-regressions.log` as failed-run evidence.
- The retry finished **4 passed in 3602.67 seconds**, including full teardown and
  helper restoration: the two remaining AWS cases plus the refined frozen-orphan
  and foreign-resolver native cases. Evidence:
  `/private/tmp/stelvio-aws-regression-retry-20261018`; log
  `/private/tmp/stelvio-aws-regressions-retry.log`. Lambda interface retirement
  accounted for much of the teardown wait; no manual AWS deletion was needed.
- Fresh-client audits confirmed absence of all five deployed proof applications
  (`stlv-d52d82`, `stlv-a8a80d`, `stlv-9546e1`, `stlv-8e5217`, `stlv-f1e54c`),
  plus the never-deployed `stlv-ff14c1` attempt. Recorded sessions, private zones,
  log groups, state versions and passphrase parameters are absent. Each evidence
  root contains `independent-audit.json`.
- The final independent live-host audit matched **both** AWS runs' initial
  routes, interface identities, resolver inode/content ownership, service and
  process inventories. Helper inspection was empty and certain. Original image
  `fd65dee8e07f72f0303a51140a7b1158dcfa11f5cc6856ebbb675a21c1516625`
  is restored. See the retry root's `independent-host-audit.json` and
  `independent-host.json`. Native Intel acceptance remains pending.

- Additional actual Python–Traforo process interoperability: **5 passed** using
  `traforo-6pntvbfo` (fixed-wire UDP/TCP DNS, retained endpoints and isolated runtime
  normal/error/owner-death paths). Log `/private/tmp/stelvio-native-process-interop.log`.
- Documentation build passed; all integration tests collect (235 total at this
  snapshot). New tier safety tests verify default skips, both-flag AWS gating,
  serial-only execution, platform/root refusal and exclusion of ordinary tiers.


### 2026-10-18 dependency update: unit-only acceptance

Updated miekg/dns to v1.1.73, tun2socks/v2 to v2.7.0, x/sys to v0.49.0,
x/net to v0.61.0, x/time to v0.16.0, x/exp to
v0.0.0-20261014143641-f0251eb4aa3e, and testify to v1.12.1. btree remains
current at v1.1.3. Go tooling regenerated go.sum and removed obsolete entries;
its retained YAML v3.0.5 and x/sync v0.24.0 checksums are current as well.

The latest gVisor main revision (9e9495997e29) failed compilation because its
Bazel source layout mixes Go package names. The corresponding Go-consumable
branch revision, v0.0.0-20261017122056-1b7906fdee8c, compiles without Traforo API
changes and includes the corrected ARM64 CAS new-value register R2. The
platform fail-fast guard was removed without weakening the stream regression.

On macOS 15.7.5 arm64 with Go 1.27.2, both complete suites passed (all six
packages), including the native ARM64 large-stream race regression:

```sh
cd pkg/traforo-client-go
go test -mod=readonly -count=1 -timeout=120s ./...
go test -mod=readonly -race -count=1 -timeout=180s ./...
```

The go.mod and go.sum SHA-256 digests were identical before and after both runs.
This supersedes the historical ARM64 race blocker for local source only. No
release pins, installed helper, host networking or AWS resources were changed;
no release build or native networking acceptance was performed. Real Intel
networking remains pending.
