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
# On ARM64 macOS, use Rosetta for race testing; see the limitation below.
(cd pkg/traforo-client-go && GOARCH=amd64 CGO_ENABLED=1 make race)

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


## Native ARM64 race blocker

The large-stream regression exposes a pinned gVisor race-only ARM64 assembly defect
(`pkg/sync/race_arm64.s`, new value loaded into R1 instead of R2). It spins TCP
workers instead of parking and can starve deadlines. The test fails early on that
combination with an explanation. It is not skipped. Run the full race suite under
amd64/Rosetta on this host; native ARM64 race acceptance remains blocked pending a
separate upstream dependency fix. No module-cache edits or dependency-pin changes
are made by this test migration.
