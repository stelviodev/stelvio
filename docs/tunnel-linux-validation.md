# Traforo Linux validation

Validated on 25 October 2026 in the dedicated Lima `debian` VM. The supported
reference environment is Debian 13 ARM64 with systemd 257 and systemd-resolved.
The Mac worktree is the only writable shared directory. Branch changes and
commits remain on macOS; the guest has separate Git metadata for provenance.

The initial frozen functional candidate is `3cf4d1194f6b31ea6ab1b58bcce9f58a8836c5f7`,
on `feat/traforo-linux`, based on monitor commit
`261cb68a62afcd39b7b1bb8334cd0d31df0f6612`. Later commits contain the developer
sync workflow, script verification, and this record.

## Results

| Check | Result |
| --- | --- |
| Ordinary Debian Python suite | 4,601 passed; 284 opt-in tests skipped |
| Frozen interoperability | 65 passed; no selected tests skipped |
| Frozen privileged Debian suite | 10 passed; no selected tests skipped |
| Acceptance-tool regressions | 92 passed on macOS and Debian |
| Linux Go unit/race, vet, lint | Passed; lint reports zero issues |
| Bounded fuzzing | Protocol, Linux snapshot, DNS, control, and diagnostics passed |
| Darwin Go unit/race | Passed, including descriptor-bearing carrier rejection |
| macOS tunnel Python regressions | 908 passed; 13 opt-in tests skipped |
| Local administration/builder regressions | 41 passed |
| Release-format builds | Darwin ARM64/AMD64 and Linux ARM64/AMD64 validated |
| Interrupted-VM recovery | Passed after a forced power cycle with a journaled session |
| Cold-boot native smoke | Passed; helper left installed and idle |
| Repository Ruff checks | Lint and formatting passed |

The native suite covers two independent VPC views, selected subnet routes and
excluded addresses, real TCP and system DNS, exact-hostname readiness, retained
DNS during outages, stale generations, owner SIGKILL, helper crash recovery,
resolved restart, concurrent administration refusal, foreign-interface refusal,
failed replacement rollback, diagnostics, and a real terminal monitor.
Interoperability includes isolated wheel imports, actual Go processes, and real
OpenSSH first trust, restart, changed-key refusal, and explicit trust reset.

The frozen acceptance command was:

```sh
traforo-acceptance run 3cf4d1194f6b31ea6ab1b58bcce9f58a8836c5f7 \
  --only native --only interop --yes
```

All five selected stages passed: preparation, build, distribution,
interoperability, and native acceptance. The independent audit certified image,
service, interface, route, DNS, resolver configuration, process, and ownership
restoration. The runner correctly reports `partial-passed`, because AWS and the
other separately run stages were not selected in that frozen round. Earlier
failed exploratory acceptance rounds remain separate records in the guest.

## Artifacts and evidence

The full frozen run is retained in the guest at:

```text
/home/sebst.guest/.local/state/stelvio/traforo-acceptance/run-kdjbhvg2
```

It contains the frozen source and observer, build provenance, both Linux images,
wheel/sdist, JUnit XML, command logs, original-image restoration evidence,
`report.md`, `report.json`, and `run.json`.

The initial installed Linux image had SHA-256:

```text
a03c06f280db58c7b8321684a36b3bcc749877a910469707f1e6ac2ee69780d7
```

The stable developer copy is `/home/sebst.guest/src/traforo-linux`, with
`traforo-linux.build.json` beside it. The initial image reported
`v0.0.7-dev+g3cf4d1194f6b`. The system copy is
`/usr/local/libexec/stelvio-traforo`.

Reboot evidence is retained under
`/home/sebst.guest/.local/state/stelvio/reboot-proof-final`. The proof froze the
helper after its journal was durable, flushed the guest, force-stopped Lima,
and restarted it. A new boot identity, unchanged image digest, absent journal,
idle helper, and restored interface/route/DNS inventories were verified.

Persistent ordinary-suite and fuzz logs are under
`/home/sebst.guest/.local/state/stelvio/linux-validation`. A private Mac copy of
the acceptance report, provenance, reboot evidence, binary, and verification logs
is available at `/private/tmp/traforo-linux-evidence`.

## Development and limits

From the Mac worktree, run `./scripts/debian/sync-to-vm.sh`. Then use
`/home/sebst.guest/src/stelvio-linux` inside Debian. The sync refreshes guest-only
Git metadata and working files, while retaining Linux virtual environments and
build outputs. The normal guest builder was exercised successfully after reboot.

For the installed candidate:

```sh
export STLV_TRAFORO_DEV_BINARY=/home/sebst.guest/src/traforo-linux
uv run stlv tunnel status
```

Live AWS acceptance, native Linux AMD64 execution, other distributions/resolvers,
and published Linux release downloads remain unvalidated. During initial delivery,
no AWS resources were deployed, credentials copied, releases published, branches
pushed, or PRs opened. Linux CI is configured for PRs and has not run on GitHub.

## Subsequent correctness and security review

The reviewed functional candidate is
`9fc8a01ce8e368ad6cfa96a06dec75f11eec4626`. It supersedes the initial candidate.
The review examined peer credentials and pidfds, namespace checks, host-resource
certificates, DNS configuration and repair, journals, and administration rollback.

The review found and fixed these issues:

- DNS repair trusted a saved interface index after link ownership changed. Every
  repair now certifies the link name, type, index, and alias before D-Bus writes.
- Periodic DNS checks missed foreign claims added after activation. Overlapping
  claims now withdraw packet readiness; repair refuses them and preserves foreign
  configuration. DNS server verification also requires the expected empty TLS name.
- Compound shell assertions could bypass checks because `set -e` does not abort
  on a failed nonfinal command in an `&&` chain. Ownership, type, symlink, and
  absence checks now exit explicitly. ACL inspection errors propagate before
  filtering, rather than being hidden by a successful pipeline tail.
- Replacement stopped a service before certifying the installation receipt and
  effective unit. It now checks image identity, unit digest, loaded fragment,
  drop-ins, and enablement links first. The helper also refuses service overrides.
- A failed first install could leave partial image, receipt, and service files.
  Receipt-before-unit persistence now certifies rollback of partial installation.
  Failed replacement restores the prior enabled and active states, including an
  installation that was disabled and stopped.

New native regressions exercise link ownership changes, a later foreign DNS
claim, changed unit contents, a nonroot-owned unit, a service drop-in, partial
first installation, and rollback of enabled/running and disabled/stopped services.
Ordinary Go tests independently exercise kernel socket credentials, UID mismatch,
pidfd birth identity, changed boot identity, and process exit.

| Review validation | Result |
| --- | --- |
| Frozen interoperability | 65 passed |
| Frozen privileged Debian suite | 17 passed |
| Independent restoration audit | All registered host checks certified |
| Ordinary Debian tunnel regressions | 899 passed; 22 opt-in tests skipped |
| Affected macOS Python regressions | 76 passed |
| Linux Go race, vet, and lint | Passed; zero lint issues |
| Linux release-format ARM64/AMD64 images | Built and validated |
| Ruff lint and formatting | Passed |
| Interrupted-VM recovery and cold-boot smoke | Passed |

The frozen run is retained at
`/home/sebst.guest/.local/state/stelvio/traforo-acceptance/run-p0bdv4yk`.
It reports `partial-passed` because only build, distribution, interoperability,
and native stages were selected. Ordinary regressions and Go checks were run
separately. Exploratory failed test runs remain recorded separately; the final
frozen run has no failures or selected-test skips.

The installed candidate and stable developer copy now have SHA-256:

```text
887ca3db6ccd452eb2f382df22438d6457322a50bfa5650089df0a42a22b019d
```

The image reports `v0.0.7-dev+g9fc8a01ce8e3`. Reboot evidence is under
`/home/sebst.guest/.local/state/stelvio/reboot-proof-review-9fc8a01c`.
Other review logs are under
`/home/sebst.guest/.local/state/stelvio/linux-security-review-9fc8a01c`.
Private Mac evidence is under `/private/tmp/traforo-linux-security-review`.
The validated helper remains installed and idle. AWS and native AMD64 execution
remain pending. No PR was opened.
