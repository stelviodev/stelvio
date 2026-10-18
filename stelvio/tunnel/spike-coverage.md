# Historical spike coverage and maintained regression tests

Updated 2026-10-18. This is a source-to-test map, not an assertion that every
scenario passed on the current candidate. Dated execution results belong in
`testing.md` and `tasks/dev-vpc.md`.

The scripts under `spikes/dev-vpc-v1` are historical experiments. They may import
removed APIs, assume temporary bastions, use the old C helper, or depend on
Python DNS handling that no longer exists. Do not run them as current installation,
cleanup, AWS recovery, or release instructions. Preserve their source and original
evidence. Maintained tests neither import these scripts nor write evidence there.

| Historical proof | Maintained replacement | Boundary |
| --- | --- | --- |
| `build.sh`, `native/broker/main.c` | GoReleaser builder; Python artifact/installation tests; privileged fresh-install and upgrade tests | Old C packaging is obsolete. Builds happen outside pytest. |
| `native/main_test.go`, `probe.py` | Go `TestTwoVPCLargeStreamsAndHalfClose`, carrier generation test; native two-VPC packet test | Concurrent distinct binary megabyte payloads, client FIN, exact full responses, unit removal/reconnect. |
| `p3_proof.py` | Go helper/Darwin/protocol tests; privileged lease/descriptor test; native DNS/packet tests | Includes active cleanup/uninstall refusal and elevated-forwarder rejection. |
| `p3_stale_proof.py`, `process_tree.py` | Privileged normal/owner-kill/frozen-orphan tests; Go parent identity and maintenance tests | Verify cleanup while the frozen child remains alive. |
| `p3_restart_proof.py` | Privileged helper-restart test; Go journal/independent-vector tests | Real launchd restart, stale-capability rejection and fresh acquisition. |
| `p3_install_recovery_proof.py` | Python transaction tests; privileged interrupted replacement and legacy migration | Real legacy migration requires a separately supplied checksum-verified legacy artifact. |
| `p4_proof.py` | AWS single/multi-VPC CLI tests and standalone checks | Real TLS/member discovery/read-write, private aliases, reconnect and teardown. |
| `p4_network_fault_proof.py` | AWS partial outage and host-key-rotation scenarios; native public-fallback rejection test | Host-key failure is terminal for that session; restore the key and start a new session. |
| `p4_environment_proof.py` | Python captured-credential/session/runtime tests; actual runtime interoperability | Runtime interoperability needs `STLV_TEST_TRAFORO_IMAGE`; do not count skips as passes. |
| `p4_dns_fallback_proof.py`, `dns_relay.py`, `local_dns_probe.py` | Go DNS tests; Python fixed-wire interoperability; privileged scoped-DNS tests | Python relay and dnspython-based proof algorithms are superseded. |
| `p6_native_cycles.py` | Privileged 100-cycle two-VPC DNS/route test | Compare host baseline after every cycle. |
| `p6_plain_proof.py` | AWS real non-VPC CLI test with the helper uninstalled | Proves ordinary dev mode does not activate managed networking. |
| `p6_monitor.py`, `p6_finalize.py` | Maintained CLI session ownership, teardown and evidence fixtures | Preserve failure artifacts; independently audit AWS absence. |
| `p2_policy_proof.py`, `p2-policy-app` | VPC component/metadata tests and AWS persistent bastion scenarios | Default-disabled/current explicit policies replace the historical policy matrix. |
| `aws_ownership.py`, `aws_recovery.py`, `aws_engine_recovery.py`, `aws_actor_preview.py`, `aws_provider_attach.py`, `aws_access_lifecycle.py`, `aws_access_interrupt.py` | Current persistent-resource tests and rejection of legacy temporary metadata | Temporary access stacks, creator fencing, provisioning recovery and session-owned infrastructure were removed. Do not port those algorithms. |
| `ssh_transport.py`, `aws_transport.py` | Python transport/process/SOCKS tests; AWS reconnect and key-change scenarios | Current production transport is authoritative. |
| `spikes/vpc-tunnel-app` | Maintained `tests/integration/fixtures/tunnel_app` | The spike remains a human example; tests use their own checked-in fixture. |
| `spikes/dev-vpc` | Historical caches and a private plugin executable remain locally | No maintained Python proof source remains in its old checks/package directories. Never use that cached plugin instead of the verified private installer. |

## Explicit execution tiers

```sh
# No host routes/DNS or AWS provisioning:
uv run pytest tests/tunnel -q
(cd pkg/traforo-client-go && make lint vet test)
# Run the race suite natively, including on ARM64 macOS.
(cd pkg/traforo-client-go && make race)

# Actual macOS networking and administration, no AWS deployment:
uv run pytest tests/integration --integration-macos-privileged -n 0 -v

# Actual macOS networking plus billable disposable AWS applications:
STLV_TEST_AWS_PROFILE=default STLV_TEST_AWS_REGION=us-east-1 \
  uv run pytest tests/integration --integration-macos-privileged \
  --integration-tunnel -n 0 -v
```

`run_all.sh` never enables either host lane, even when `STLV_TEST_TUNNEL=1`.
The new flag is required for every host-mutating test. The AWS flag additionally
enables AWS scenarios. Ordinary test runs skip both. Do not combine these lanes
with other integration flags or workers.

The host fixture locks a stable root-owned directory across users, refuses live or
uncertain ownership, preserves the installed image, and restores installation
presence/digest after the lane. Helper receipts are recreated by the real installer;
receipt inode identities need not match the previous installation. Stable routes,
interfaces, resolver ownership and service presence/content must match baseline.
Ephemeral ARP/neighbor routes are recorded but excluded from equality checks.

For cross-venv acceptance set `STLV_TEST_STELVIO_WHEEL` to an absolute candidate
wheel path. For legacy migration set `STLV_TEST_LEGACY_HELPER` and
`STLV_TEST_LEGACY_SHA256` to an independently approved artifact and digest.
Missing fixture prerequisites are explicit skips and leave acceptance pending.
No production fault switches are introduced. Fault scripts belong to the tests.

All generated applications, logs, inventories and proof ownership are written
under pytest's temporary directory. Use a unique `--basetemp` per run and retain it
until teardown and independent audits finish. Pytest can remove an existing
`--basetemp`, so never reuse a directory holding unresolved ownership evidence.
Native Intel and any unexercised macOS version remain pending regardless of skips,
Rosetta execution, or earlier arm64 evidence.


## Historical native ARM64 race blocker

Before the dependency update, the large-stream regression exposed a pinned gVisor race-only ARM64 assembly defect
(`pkg/sync/race_arm64.s`, new value loaded into R1 instead of R2). It spins TCP
workers instead of parking and can starve deadlines. The test fails early on that
combination with an explanation. It is not skipped. Run the full race suite under
amd64/Rosetta on this host; native ARM64 race acceptance remains blocked pending a
separate upstream dependency fix. No module-cache edits or dependency-pin changes
are made by this test migration.

The subsequent dependency update selects gVisor's Go-consumable branch at
`v0.0.0-20261017122056-1b7906fdee8c`, which loads the new value into R2 correctly.
The ARM64 fail-fast guard is removed; use native `make race` now. This source
update does not change release downloads or establish new host/AWS acceptance.


## Runbook coverage beyond historical spikes

| Runbook gate | Maintained regression | Remaining prerequisite |
| --- | --- | --- |
| Wheel/sdist exclusions and isolated installed CLI | `tests/tunnel/test_distribution_acceptance.py` | Supply absolute wheel/sdist paths; no builds inside pytest. |
| Actual pinned release assets, both architectures | `tests/tunnel/test_release_acceptance.py` | Explicit live-download opt-in; Rosetta/native execution where supported. |
| Genuine release-to-local upgrade with active traffic | Native `test_pinned_release_to_local_candidate_upgrade` | Verified v0.0.5 plus a distinct developer candidate. |
| Replacement interruption at staging, retirement, publication | Parametrized native changed-image test | Explicit privileged tier. |
| Effective scoped DNS baseline | `MacOSHost` raw/normalized DNS evidence; `test_host_evidence.py` | Dedicated macOS host; bounded dynamic-store convergence. |
| Graceful standalone exit and abrupt death | Both single-VPC AWS policies | Explicit AWS and privileged flags; full teardown. |

An automated scenario existing is distinct from its current candidate passing.
See the latest dated testing-guide evidence, including skips and restoration.
