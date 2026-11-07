# Tunnel diagnostics validation

Date: 2026-10-23. Base: `feat/traforo-13` at `ba998c4451e00fc1e182da272f8c836953b4589a`.
Platform: macOS 15.7.5, Apple Silicon. Python 3.12; uv 0.13.5;
Go 1.27.3; GoReleaser v2.18.3; Apple SDK 15.5.

## Local checks

- Structured report schema 2 update: full Python suite 4,498 passed, 258 skipped.
  Diagnostics parser, IPC, and table tests: 69 passed. Fixtures cover every system
  collector, partial parsing, truncation, nested lookups, private data exclusion,
  and complete tables at widths 40, 100, and 180. Ruff and whitespace checks passed.
- Read-only macOS CLI smoke check: both output modes passed outside a project.
  The host supplied 25 interfaces, 647 routes, and nine DNS resolvers; all parsed
  without warnings. Packet-filter access was reported unavailable. Raw host
  reports are local evidence and are not included in the repository.
- Python tunnel, CLI, dev admission, and Function dev-mode regression suite:
  840 passed, 13 opt-in/platform cases skipped.
- Explicit isolated runtime/forwarder interoperability: 3 passed. Covers normal
  shutdown, owner loss, cleanup failure, private snapshots, and captured context.
- Explicit wheel/source archive acceptance: 3 passed. Installs the wheel in an
  isolated environment, checks command registration and JSON output outside a
  project, and confirms diagnostics does not create the binary cache.
- Python lint, formatting, and diff whitespace checks passed.
- Go unit and race suites passed. `make format lint vet` passed with no issues.
- Diagnostics JSON parser fuzzing passed (10 seconds). The bounded binary request
  decoder fuzz check also passed (30 seconds).
- Both Darwin images built through the existing build-only workflow. No release
  was published, and release pins remain unchanged.

The opt-in runs above are separate from the broad regression run. No AWS
resources were deployed and no AWS acceptance run was performed.

## Native candidate and restoration

Go candidate commit: `2b142668dc8e92c035725efa982b4fba436c4174`.
Later Python and documentation changes do not change these Go inputs.

Go input SHA-256:
`ec769a1d34f3c8686f66c089eb30e20104e736cac8ff7dfd504757a9237b8ac9`.

| Image | SHA-256 |
| --- | --- |
| Darwin arm64 | `0b8e0084f420e6cc06e6230d607dc350cc7f3c7c967051edce19c3b29545d8b1` |
| Darwin amd64 | `66490ca3f90d8c493a62c0ab62b1a551162d160c9782fb5bf558e6d378a12fd5` |

The serial native snapshot test passed on the final candidate. A separate process
read helper diagnostics during an owned session. Closing forwarder statistics
left the process and mutation control usable. The fixture verified restoration of
the original installed helper bytes and independent host baseline.

Full two-unit host traffic/counter acceptance remains pending. An earlier attempt
stopped at configure stage 4: an existing foreign VPN route covers `192.0.2.x`,
which the helper requires for its point-to-point interfaces. The helper and host
baseline were restored after that attempt. The VPN was left unchanged. The report
now identifies such route observations. The native two-unit test remains present
for a host without this conflict. Go tests verify two-unit streams, exact stream
opening counts, reconnect continuity, and concurrent statistics without host routes.

Native Intel hardware and macOS 26 acceptance remain pending. Image build and
version validation do not replace those runs. The pinned v0.0.7 artifact cannot
supply the new statistics; publication and pinning of a compatible release remain
a separate release step.

## Reproduce

Use the commands in `stelvio/tunnel/testing.md` for Python and native checks.
From `pkg/traforo-client-go`, run `make test race vet lint fuzz`, then run
`go test ./internal/diagnostics -run '^$' -fuzz '^FuzzDecode$' -fuzztime=10s -parallel=2`.
Build with the repository's pinned toolchain before setting
`STLV_TRAFORO_DEV_BINARY` and `STLV_TEST_TRAFORO_IMAGE` for explicit interoperability.
Supply `STLV_TEST_STELVIO_WHEEL` and `STLV_TEST_STELVIO_SDIST` to
`tests/tunnel/test_distribution_acceptance.py` for archive and installed CLI checks.
