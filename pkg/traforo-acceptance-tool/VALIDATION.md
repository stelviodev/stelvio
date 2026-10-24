# Validation record

This record covers implementation checks for the acceptance tool. It does not
claim that native or AWS acceptance has passed through the new tool.

## Candidate and environment

- Candidate: `f87bd5bcb6c155c875b1ac8685da1fda74b821c9` (PR #398).
- Patches: none. PR #378 was not applied.
- Host: macOS 15.7.5, Apple Silicon; macOS SDK 15.5.
- Go: 1.27.3. GoReleaser: 2.18.3.
- Source fingerprint: `72afb089ec6105491e225a9f46bbce1a4d196c52976c6f8720434d83e3dc02d6`.
- Run: `traforo-acceptance-dqnj3l_f`, retained in the host's private user-temporary directory.
- Archived runner/observer fingerprint: `2720d1b4d3490627c5b287ca29102a7306016e74a61a82dc5b4243e8f50a66e5`.
- Outcome: **partial-passed**. All 11 selected nonprivileged stages passed.

Command, from the tool checkout:

```sh
uv run --project pkg/traforo-acceptance-tool traforo-acceptance \
  run f87bd5bcb6c155c875b1ac8685da1fda74b821c9 \
  --only local --only build --only interop
```

## Results

| Check | Result |
|---|---|
| Tool unit tests | 46 passed; no host or AWS mutations |
| Tool lint and format check | Passed |
| Tool wheel and sdist build | Passed |
| Tool wheel import and CLI outside checkout | Passed in a clean temporary environment |
| Candidate Python tests | 4,535 passed, 258 expected opt-in/platform skips |
| Candidate Python lint and format checks | Passed |
| Candidate Go unit and ARM64 race suites | Passed with readonly dependencies |
| Candidate Go vet and lint | Passed |
| Five bounded Go fuzz targets | Passed, 30 seconds each |
| Both local Darwin builds and provenance | Passed |
| amd64 command execution through Rosetta | Passed; does not prove Intel networking |
| Candidate wheel/sdist and actual-process interoperability | 65 passed |
| Independent read-only macOS inventory | Passed, including admin-lock metadata and DNS capture; no live cleanup proof |
| Native/AWS test collection with external observer | 22 selected, 1 release-upgrade case deselected; no fixtures executed |
| Traforo release downloads | Not run; intentionally excluded |
| Native installer and host mutation | Not run; acceptance pending |
| AWS deployment and independent live absence audit | Not run; acceptance pending |

Local artifact identities:

| Architecture | Bytes | SHA-256 |
|---|---:|---|
| arm64 | 8,203,026 | `832b36943b3ed771e229a77392d6a65292a6e341119f978a75e9be558f5e471f` |
| amd64 | 8,701,008 | `e0128f6d657db159caeb4f5207e991d9e164d8d2345b594609ba99d19ec7a6f3` |

The Stelvio archives excluded the Go implementation, native executable assets,
and this standalone acceptance package. No helper was installed or replaced.
No proof application was deployed. Existing AWS and host installations were not
changed. Empty live-audit findings in this partial run mean no live stage was
activated; they do not certify the machine's unrelated state.

Host-audit and administration-recovery refinements after the archived run were checked
with fake installer transactions. Their real native/AWS execution remains pending.

## Fault coverage

Package tests cover ordered/conflicting/empty patches, private run directories,
concurrent locks, source/artifact drift, local-binary environment selection,
command timeouts, changed process identities, immediate teardown reporting,
JSON-safe redaction, deterministic reports, SDK request models and budgets,
partial deployment ownership, account mismatch, foreign resources, inventory
gaps, authorization failure, active-image refusal, interrupted replacement,
original-image restoration, and restoration of initial helper absence.

Earlier trial runs remain available as failed evidence. They exposed observer
interference from candidate OS mocks, an unsafe ancestor for OpenSSH StrictModes,
a candidate assertion affected by a directory named `tests-python`, and redaction
of serialized JSON delimiters. The tool now has regression coverage for these
boundaries. The passing run used a fresh directory; failed records were not
rewritten as passes.

## Next live run

Run the native suite with explicit host authorization, then the Lambda stage and
the four existing AWS stages with the expected account. Check the independent audit and retain the complete
report. Native Intel and macOS 26 acceptance remain pending. A local/mock pass is
not a substitute for these runs.


## Multi-VPC Lambda stage — 2026-10-24

This section records nonprivileged checks for `aws-lambda-vpc`. Live execution of
this stage remains **pending**. The earlier local-build results above do not prove
that this new stage, recovery, or independent live audit passes on AWS.

Candidate:

- Base: `f87bd5bcb6c155c875b1ac8685da1fda74b821c9`.
- Ordered patches: `ea1d914483baf87ae43ec569e041d082bb8f799d`, then
  `e196a31d46bc90e9857f3c72b89ed58c9a64cc3a`.
- Resulting diff SHA-256: `b1180c1722b68f11ad071fa1cb7244cae4c9296bf68a55c4e39dbe76531dac3f`.
- Source fingerprint: `bdd164cd39953f722013eab8667b8d7e16164456d85fee64746bd7215c57909b`.
- Evidence directory: `traforo-lambda-stage-validation-3cp5k0jf` in the host's
  private user-temporary directory. It contains candidate identity, timestamped
  command logs, and collection output. The candidate remained unchanged.

| Check | Result |
|---|---|
| Tool unit tests | 72 passed |
| Tool lint and format check | Passed |
| Tool wheel and sdist | Built; both new observer/audit modules included |
| Candidate adapter validation | Passed with both explicit patches |
| PR #411 fixture unit tests | 26 passed; no integration fixtures executed |
| Lambda suite collection with external observer | Exactly five tests |
| Lambda plus separate non-VPC collection | Exactly six tests |
| Native helper installation and host networking | Not run; pending |
| Lambda AWS deployment, fault scenarios, recovery and absence audit | Not run; pending |

The unit tests cover default/partial stage selection, one-process five-case
execution, subclass hooks without duplicate registration, independent API intents
and receipts, partial setup, lost responses, bounded explicit-throttle retries,
foreign-resource refusal, independent peer/application recovery, endpoint and
interface inventory, per-VPC fault evidence, SDK request models, and unchanged
DocumentDB behavior. Empty lists do not certify uncertain API creation.

No Go source, release pins, helper installation, host networking, AWS deployment,
or PR #411 files were changed. The new stage uses the existing build and
distribution prerequisites; no new Go compilation was needed for these checks.

Reproduce collection without running integration fixtures, after preparing the
candidate and installing its locked dependencies:

```sh
uv run pytest tests/tunnel/test_lambda_acceptance_fixture.py -n 0
uv run pytest tests/integration/test_tunnel_lambda.py \
  --integration-macos-privileged --integration-tunnel -n 0 --collect-only -q
uv run pytest tests/integration/test_tunnel_lambda.py \
  tests/integration/test_tunnel.py::test_non_vpc_actual_dev_runs_without_installed_helper \
  --integration-macos-privileged --integration-tunnel -n 0 --collect-only -q
```

The retained collection commands also load the tool's external pytest observer.
Use the README command to run the actual stage only after reviewing its AWS costs,
permissions, ownership checks, and authorization requirements.
