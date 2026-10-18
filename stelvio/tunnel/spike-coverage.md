# Tunnel regression coverage

This matrix maps current behavior to maintained tests. Test existence is distinct
from a particular candidate passing. Use [testing.md](testing.md) for the ordered
runbook and record results, skips, binary identities, and cleanup evidence per run.

| Behavior | Maintained coverage | Execution prerequisite |
| --- | --- | --- |
| Concurrent two-VPC large streams, half-close, complete responses, unit removal/reconnect | Go `TestTwoVPCLargeStreamsAndHalfClose` and carrier generation tests | Go unit/race suite on macOS |
| DNS parsing, ownership, truncation, retained listener ports and stale revisions | Go DNS/control tests and fuzz targets | Go tests |
| Helper parsing, descriptor abuse, journal corruption/recovery and parent identity | Go helper/protocol/Darwin tests, independent vectors | Go unit/race suite |
| Pinned downloads, authentication, corruption, concurrent caching and developer override | `tests/tunnel/test_native_assets.py`, `test_development.py`, `test_plugin.py` | Offline Python tests |
| Deployment-owned bastions, warning-and-run, metadata validation and handler selection | Component, manifest, discovery, session and standalone Python tests | Offline Python tests |
| Captured environment and actual Python–Traforo DNS/runtime control | Python runtime/forwarder/DNS interoperability | `STLV_TEST_TRAFORO_IMAGE` points to a verified candidate |
| Wheel/sdist exclusions and isolated installed CLI | `test_distribution_acceptance.py` | Absolute `STLV_TEST_STELVIO_WHEEL` and `STLV_TEST_STELVIO_SDIST` |
| Both real release assets, version, libraries and cache reuse | `test_release_acceptance.py` | `STLV_TEST_TRAFORO_RELEASE_DOWNLOADS=1`; Rosetta/native execution where supported |
| Scoped DNS and packet carrier, two-unit isolation and reconnect | Native `test_tunnel_dns.py` | Privileged macOS tier |
| Normal exit, owner SIGKILL, frozen orphan, helper restart, stale capabilities | Native `test_tunnel_host.py` | Privileged macOS tier |
| 100 two-unit lifecycle cycles and baseline restoration | Native lifecycle test | Privileged macOS tier |
| Fresh install, cross-venv sequential access and competing-session refusal | Native administration tests | Privileged tier; candidate wheel for cross-venv |
| Pinned-release-to-local upgrade with active traffic | `test_pinned_release_to_local_candidate_upgrade` | Privileged tier; distinct `STLV_TRAFORO_DEV_BINARY` candidate |
| Interrupted replacement at staging, retirement and publication | Parametrized native transaction test | Privileged macOS tier |
| Foreign resolver protection, cleanup retry and uninstall separation | Native administration tests and Python transaction tests | Privileged tier for OS behavior |
| Effective scoped DNS baseline | Raw/normalized `scutil --dns`, `test_host_evidence.py` | Dedicated host; bounded dynamic-store convergence |
| DocumentDB TLS/member discovery/read-write, standalone reconnect, SIGINT/SIGKILL | Single-VPC AWS scenarios (`True` and `{}`) | AWS and privileged tiers |
| Independent VPC failure, admission and SSH host-key change rejection | Multi-VPC AWS scenario | AWS and privileged tiers |
| Successful non-VPC CLI invocation without a helper | Non-VPC AWS scenario | AWS and privileged tiers |
| Bastion retention on session exit, application teardown and independent absence | AWS teardown/ownership fixtures | AWS and privileged tiers |

## Execution tiers

```bash
uv run pytest tests/tunnel -q
make -C pkg/traforo-client-go lint vet test race

# Host networking and administration, no AWS deployment:
uv run pytest tests/integration --integration-macos-privileged -n 0 -v

# Adds disposable, billable AWS applications:
STLV_TEST_AWS_PROFILE=default STLV_TEST_AWS_REGION=us-east-1 \
  uv run pytest tests/integration --integration-macos-privileged \
  --integration-tunnel -n 0 -v
```

`run_all.sh` never enables host-mutating tests. The two tunnel flags are the only
integration flags that may be combined; workers and other tiers are rejected.
The host fixture serializes across users, refuses live/uncertain ownership,
preserves the original installed image, and restores host state. Missing candidate
artifacts or execution support produce named skips and leave acceptance pending.

Applications, logs, receipts, and inventories use pytest temporary directories.
Keep a unique `--basetemp` until cleanup audits finish; never reuse a directory
holding unresolved ownership evidence. No production fault switches are used.
Test-only fault scripts must retain recovery evidence on failure. Rosetta command
execution does not establish native Intel networking acceptance.
