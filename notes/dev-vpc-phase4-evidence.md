# Dev VPC Phase 4 evidence

Date: 2026-10-02. Host: macOS darwin 24.6.0. No AWS resources were created. No sudo. No live packet filter, no live `/etc/resolver`, no live Linux systemd-resolved, and no kernel `DIOCNATLOOK` or `SO_ORIGINAL_DST`.

Phase 4 records acceptance against the plan's A01–A15 matrix and writes public docs and the changelog. It does not rerun Phase 0's destroyed spike, and it does not turn a unit test into a live pass. Verdicts below follow the earlier evidence files. A later phase does not upgrade a case that Phase 0, 2, or 3 marked FAIL or BLOCKED.

## Release statement

Managed host networking is not accepted for release. No OS family passed live packet-filter and split-DNS integration. External mode is the connectivity path that does not modify host routes or DNS. Bastion infrastructure can still be deployed when the user sets `bastion=True`. That creates billable EC2 resources and does not by itself prove workstation reachability.

`stlv dev` (auto) and `stlv dev --network managed` select that managed session when one used VPC has bastion metadata. The shipped session's default connector does not open SSH, and the default helper applicator returns `applied: false` and does not call `pfctl`. Startup then fails closed with `network_not_ready` after the deploy, and the deployment is left intact. `rules_applied: true` from a test double is not a live packet filter. sshuttle is not packaged and is not a forwarder.

## Pinned versions

These pins are copied from the earlier evidence files and from the code those files describe. Phase 4 did not remeasure them on a new host, and it did not resolve a new AMI.

| Piece | Pin | Where recorded |
| --- | --- | --- |
| Phase 0 spike Python | 3.13.11 | `notes/dev-vpc-phase0-evidence.md` |
| Phase 2 and 3 Python | 3.12.11 | `notes/dev-vpc-phase2-evidence.md`, `notes/dev-vpc-phase3-evidence.md` |
| Host OS for those runs | macOS darwin 24.6.0. Phase 0 also recorded 15.7.5 arm64 | Phase 0, 2, and 3 evidence |
| dnspython | 2.8.0 | Phase 0 and Phase 2 evidence. `pyproject.toml` pins the same version |
| Remote Python | 3.11.16 at `/usr/bin/python3.11`. `/usr/bin/python3` stayed 3.9.25 | Phase 0 evidence. Code installs `python3.11` and does not replace `/usr/bin/python3` |
| AMI lookup | SSM parameter `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64` | Phase 0 evidence and `stelvio/aws/bastion.py` |
| Phase 0 us-east-1 image | `ami-065b1b834d2a83a7a`, `al2023-ami-2023.12.20260930.0-kernel-6.18-arm64`, 2026-09-30 | Phase 0 evidence. One region's tested image, not a worldwide AMI id |
| Instance type | `t4g.small`. `t4g.nano` was OOM-killed during the Python 3.11 install | Phase 0 evidence |
| Helper protocol | `protocol_version` 1, `helper_version` `"1"` | Phase 2 evidence and `stelvio/dev/constants.py` |
| OpenSSH on the Phase 0 Mac | 9.9p2, LibreSSL 3.3.6 | Phase 0 evidence |
| Session Manager plugin in the spike | 1.2.835.0 | Phase 0 evidence |
| boto3 / botocore in the spike | 1.43.107 | Phase 0 evidence |

No AWS prices are pinned. sshuttle 1.3.2 and 2.0.0 were inspected in Phase 0 and withdrawn. They are not a supported forwarder.

## A01–A15

| Case | Verdict | Evidence |
| --- | --- | --- |
| A01 Basic path | FAIL | Phase 0 reached TCP echo listeners with OpenSSH local forwards on a ControlMaster. Those forwards were not a transparent redirect, sshuttle was not the forwarder, and no dev Function was invoked through a deployed AWS entry point. Phase 2 and Phase 3 left the redirect-plus-proxy path unmet. Phase 4 did not deploy. |
| A02 DocumentDB | BLOCKED | Phase 0 created no cluster and did not disable TLS. No later phase ran a DocumentDB session, member discovery, or a nondefault port against a live cluster. |
| A03 Discovery/DNS | FAIL | Phase 0: an associated private zone stayed NXDOMAIN over TCP and UDP to `169.254.169.253` and to the VPC resolver, while `example.com` answered. Phase 2 marked the case FAIL. Relay allowlist, REFUSE, SERVFAIL, and UDP truncation are unit tests against a scripted upstream. Live OS resolver and application-client discovery were not run. |
| A04 DNS scope | BLOCKED | Phase 2 blocked live confirmation because resolver files were not installed. Allowlist, REFUSE, no public fallback on tunnel loss, and overlapping-suffix rejection are unit tests. Phase 0's relay-scope result used a loopback relay, not an installed OS resolver. |
| A05 Ownership | BLOCKED | Phase 3. Unit tests keep the supervisor up across admission and an AppSync reconnect, and READY requires `rules_applied`, not a child PID. Reload, idle, a long handler, and a debugger pause were not run against a live network subprocess. |
| A06 Failure/reconnect | BLOCKED | Phase 3. Unit tests close readiness on transport loss and name the status code. Live SSH, SSM plugin, credential expiry, session-duration exhaustion, and workstation-network interruption were not run. No fresh authenticated recovery was executed. |
| A07 Admission/deadline | PASS | Unit tests in the Phase 3 run (190 passed, including `tests/dev`, `tests/bridge/test_dispatch.py`, and `tests/bridge/test_stub.py`). Events before readiness, during recovery, after the queue budget, and past 32 queued events are rejected. A far-future Lambda epoch does not extend the budget. Reconnect does not replay a handler. This is not a live AWS entry-point run. |
| A08 Cleanup | BLOCKED | Phase 2. Lease identity, PID reuse, parent death, foreign-file preservation, and rollback passed in temporary directories. Live pf and `/etc/resolver` cleanup were not run. `sudo -n` on the Phase 0 and Phase 2 Macs required a password. |
| A09 Infrastructure | BLOCKED | `tests/aws/test_vpc_bastion.py::test_vpc_bastion_false_adds_no_bastion_resources` shows `bastion=False` creates no instance and no dev security group. `DevSession.stop` does not delete AWS resources. No live check was run that stopping `stlv dev` leaves the instance, that destroy removes it, or that session shutdown leaves NAT routes alone. The "stop does not destroy" sentence in the docs is that code contract. |
| A10 Authentication | BLOCKED | The full case includes EIC refresh on reconnect, which Phase 2 did not run. Phase 0, on a spike that was destroyed afterward, recorded zero ingress, strict host-key rejection, EIC denied for `ec2-user`, `AWS-RunShellScript` denied, the fixed document allowed, and `stlv-tunnel` locked with no sudo. Phase 2 client-policy tests do not call AWS and do not expand that spike. |
| A11 Security groups | BLOCKED | `tests/aws/test_document_db.py::test_document_db_bastion_admits_dev_sg_on_custom_port` checks a mocked ingress rule on the cluster port (including a customized port) and keeps the app security group rule. No live security group was read back. The lack of per-Function local isolation is documented from the VPC-level dev security group. It was not demonstrated on AWS. |
| A12 Regression | PASS | Unit regression in the Phase 3 run. A non-VPC endpoint runs while the managed tunnel is reconnecting. External mode starts no tunnel and does not change routes or DNS. The credential snapshot is taken before a handler's temporary environment. A live external VPN was not used and is not part of this pass. |
| A13 Boundaries | PASS | Phase 2 preflight unit tests: overlapping routes, two used VPCs, a second host session, an incompatible helper version, and broad CIDRs are rejected. An unused VPC and a default route alone are ignored. Unsupported OS is the platform check (`darwin` and `linux` only). No live route table was modified. Live Linux was not run. |
| A14 Long-running execution | PASS | Phase 3 unit tests. A timed-out executor keeps the environment and the admission lock until the thread returns. The next handler does not overlap it. The late result is logged as outlived and is not returned. `stop` does not wait for that thread. |
| A15 Extension contract | PASS | `tests/aws/test_dev_network.py` (`PrivateProbe`) publishes `_dev_resource` without a DocumentDB type check. `tests/test_stack_outputs.py::test_dev_metadata_is_hidden_from_rendered_output_and_extracted` keeps underscore keys out of rendered output and still reads them from raw state. This is not a live private connection. |

Phase 2's `tests/dev` run was 71 passed. Phase 3's combined run was 190 passed. Phase 4 did not treat those counts as live acceptance.

## R01–R15

| Requirement | Cases | Accepted? |
| --- | --- | --- |
| R01 Private and isolated client path through the bridge | A01 FAIL, A02 BLOCKED | No |
| R02 Original host, port, TLS, and member discovery | A02 BLOCKED, A03 FAIL | No |
| R03 Selected private DNS, unrelated DNS unchanged | A03 FAIL, A04 BLOCKED | No |
| R04 Session ownership beyond process existence | A05 BLOCKED, A06 BLOCKED | No |
| R05 Readiness gate and visible failure without replay | A06 BLOCKED, A07 unit PASS | No. Admission is unit-tested. Live reconnect is not |
| R06 Cleanup of owned local state, including crashes | A08 BLOCKED | No |
| R07 Explicit bastion, stop does not destroy it or NAT | A09 BLOCKED | No as a live case. `bastion=False` is the default and is unit-tested. Stop-does-not-destroy is the session code, not a live destroy test |
| R08 No public SSH, verified host keys, session credentials | A10 BLOCKED | No as the release case. Phase 0's narrower spike checks stay in that file |
| R09 Dev security group on the real service port | A11 BLOCKED | No as a live case. The mocked DocumentDB rule is unit-tested |
| R10 Network credentials independent of handler env | A10 BLOCKED, A12 unit PASS | No as a full live case. The credential snapshot unit test passed |
| R11 Non-VPC dev and external mode stay available | A12 unit PASS, A13 unit PASS | Unit only. A live external VPN was not used |
| R12 One used VPC, one managed host session | A13 unit PASS | Unit preflight only. No live route table was modified |
| R13 Deadline and uncancelable local work | A07 unit PASS, A14 unit PASS | Unit only |
| R14 Extensible private metadata, no DocumentDB type check in the runner | A15 unit PASS | Unit only |
| R15 Real OS and AWS, including TLS and replica discovery | A01–A15 | No. Managed host networking is not accepted for release |

## What Phase 4 changed

Public docs: `docs/docs/components/aws/vpc.md`, `docs/docs/concepts/dev-mode.md`, `docs/docs/components/aws/document-db.md`, and `docs/docs/changelog.md`. They describe the bastion opt-in, the dev-session controls, and the limits in the release statement. They do not say a live split-DNS or packet-filter path works.

`tests/integration/test_dev_mode.py` gains a skipped test so a `dev=True` Function URL run is not read as A01–A15. The skip does not deploy AWS. `tests/dev/test_session.py` already launches `DevSession` against fakes, including external mode, `rules_applied` false, and admission.

## What was not run

No `stlv deploy`, no `stlv destroy`, no DocumentDB cluster, no sudo, no `pfctl`, no live `/etc/resolver`, no live systemd-resolved, no kernel original-destination lookup, no external VPN, and no sshuttle.
