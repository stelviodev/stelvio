# Phase 0 evidence — dev VPC transport and DNS

Date: 2026-10-02. Host: macOS 15.7.5 arm64 (darwin 24.6.0). Spike only. No `stelvio/` modules, docs, or changelog were changed. Nothing was committed.

**Exit verdict: FAIL.** Stock sshuttle cannot meet privilege separation. Revise that part of the design before Phase 2. Do not fall back to `sshuttle --dns`, `StrictHostKeyChecking=no`, or disabled TLS verification.

Phase 1 may proceed only for bastion, security group, IAM, fixed SSM document, and metadata work that does not assume sshuttle's sudo model and does not treat private-hosted-zone answers over TCP as already proven.

## Pinned set

| Piece | Pin |
| --- | --- |
| Spike Python | 3.13.11 (Homebrew, `spikes/dev-vpc/.venv`) |
| Host OS | macOS 15.7.5 arm64, darwin 24.6.0 |
| OpenSSH | 9.9p2, LibreSSL 3.3.6 |
| dnspython | 2.8.0 |
| sshuttle | 1.3.2 inspected; 2.0.0 inspected the same way. Pin is not approval to elevate it. |
| boto3 / botocore | 1.43.107 |
| pytest | 9.1.1 |
| Session Manager plugin | 1.2.835.0, mac arm64, extracted without sudo into `spikes/dev-vpc/session-manager-plugin/` (gitignored) |
| AMI | `ami-065b1b834d2a83a7a`, `al2023-ami-2023.12.20260930.0-kernel-6.18-arm64`, arm64, root `/dev/xvda`, SSM parameter `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64` |
| Bastion type | `t4g.small` (see failed nano attempt below) |
| Listener type | `t4g.nano` |
| Remote Python | 3.11.16 at `/usr/bin/python3.11`; system `/usr/bin/python3` stayed 3.9.25 |
| Account / region | 535368238919, us-east-1. Caller was the account root. |

Local checks: `cd spikes/dev-vpc && .venv/bin/python -m pytest` — 19 passed.

## Acceptance

| Item | Result | Why |
| --- | --- | --- |
| A01 | PASS for the SSH tunnel; sshuttle not run | Local forwards on the ControlMaster reached `stlv-private-ok` and `stlv-isolated-ok`. Neither subnet had a NAT gateway or an IGW route, so this does not separately prove a NAT-backed private subnet. sshuttle transparent redirect was not executed. |
| A02 | BLOCKED | `describe-db-clusters` returned no clusters. None was created. TLS verification was not disabled. |
| A03 | FAIL for private names; relay prototype PASS | DNS-over-TCP and UDP to `169.254.169.253` and to the VPC base+2 resolver returned real answers for `example.com` (NOERROR, 2 answers) and NXDOMAIN for an associated private zone. The loopback relay returned that NXDOMAIN, REFUSED `example.com`, and truncated oversized UDP in unit tests. Live macOS `/etc/resolver` was not installed. |
| A04 | PASS for relay scope | Unrelated names are REFUSED and not forwarded. After the SSH master exited, the relay returned SERVFAIL and the system resolver still resolved `example.com`. Overlapping-suffix rejection is covered by unit tests. |
| A08 | PASS for the helper prototype; live OS state BLOCKED | Setup/update/teardown only, foreign resolver files kept, PID reuse drops the lease, parent death tears leases down. No live pf rules or `/etc/resolver` files were installed, because `sudo -n` requires a password (`EACCES` on `/etc/resolver`). |
| A10 | PASS | Bastion security group had zero ingress. Wrong host key was rejected under strict checking. EIC for `ec2-user` and `AWS-RunShellScript` were `AccessDeniedException`. The fixed document was allowed. `stlv-tunnel` is password-locked (`LK`), not in `wheel`, and `sudo -l` says it is not allowed to run sudo. The private key was not in the result JSON. |

## What the live run proved

Final successful transport run (zone `stlv-phase0.internal`, bastion `i-0c8ee93a1d76ae061`, destroyed afterward):

- Fixed SSM document, no parameters, returned the Ed25519 host key, bootstrap marker, and both Python versions. `sshd -T` shows `AuthorizedKeysCommand /opt/aws/bin/eic_run_authorized_keys %u %f`.
- EC2 Instance Connect `SendSSHPublicKey` for `stlv-tunnel` returned success. The same IAM user was denied for `ec2-user` and for `AWS-RunShellScript`.
- OpenSSH over SSM authenticated with the ephemeral Ed25519 key. Options included `StrictHostKeyChecking=yes`, `BatchMode=yes`, `IdentitiesOnly=yes`, `IdentityAgent=none`, `ForwardAgent=no`, `RequestTTY=no`, `ServerAliveInterval=15`, `ServerAliveCountMax=3`. Control socket was private. `ssh -O check` reported `Master running`.
- Remote command over that master printed `3.11.16` from `/usr/bin/python3.11` and `3.9.25` from `/usr/bin/python3`.
- A second connection with a different pinned host key failed with `Host key verification failed`.
- Bastion security group ingress rule count was 0. Egress was tcp/443 to the internet, tcp/8080 to the VPC CIDR, and DNS tcp/udp to the resolver addresses only.
- `stlv-tunnel` password status `LK`, only group `stlv-tunnel`. An earlier probe treated `sudo -n -l -U` exit code 0 as "has sudo". That exit code is wrong: the text is `User stlv-tunnel is not allowed to run sudo`.
- DNS relay on 127.0.0.1, high port, suffix allowlist: `example.com` was REFUSED; after `ssh -O exit`, the private name was SERVFAIL; `getaddrinfo("example.com")` on the Mac still returned public addresses.
- Private key material was absent from the redacted results. Session directory mode 0700, key mode 0600, key not passed in the environment.

## Private hosted zone — not proven

Route 53 had a private zone associated with the test VPC (`list-hosted-zones-by-vpc` showed it). `enableDnsSupport` and `enableDnsHostnames` were both true. The A record `echo.stlv-phase0.internal` pointed at the private listener. The hand-built DNS query matches dnspython byte for byte.

From the bastion, UDP and TCP to `169.254.169.253:53` and to `10.70.0.2:53` (VPC base + 2):

- `example.com` A: rcode 0, 2 answers, on both transports and both addresses.
- The private A record and the zone SOA: rcode 3, 0 answers, on both transports and both addresses, across 13 attempts in 60 seconds, and again later through the SSH local forward.

The same NXDOMAIN happened earlier for `stlv-phase0.test`. The account has no Route 53 Profiles and only the auto-defined recursive rule for `.`. This was not fixed by disabling TLS, disabling host-key checks, or turning on `sshuttle --dns`.

DNS-over-TCP to the VPC resolver works as a channel. Private-zone answers were not observed. Do not treat that half of A03 as settled.

## sshuttle privilege separation — design failure

Inspected sshuttle 1.3.2 and 2.0.0. `FirewallClient` elevates with `sudo` and, by default, `PYTHONPATH` pointed at the package. The published sudoers template ends the command alias with `*`, and `--ssh-cmd` can run as root. The firewall protocol accepts client-supplied routes and can rewrite `/etc/hosts`. There is no setting that points elevation at an external restricted helper.

`sudo -n true` on this Mac failed with `sudo: a password is required`, so sshuttle's pf method was not run. That missing run is not the design failure. The design failure is the elevation model itself.

The spike helper accepts only `setup`, `update`, and `teardown`, rejects executable and shell fields, rejects prefixes shorter than /16, and does not install a default route. That is the shape Phase 2 should implement. A native TUN helper remains a later revision, not a second implementation in this phase.

## Local proofs that did not need AWS

- DNS relay binds 127.0.0.1 only, UDP and TCP on one high port, suffix allowlist, REFUSED outside the suffix, SERVFAIL when the upstream is down, TC when the UDP answer does not fit.  Not an open recursive resolver.
- macOS resolver files use a unique `stlv-<nonce>-<index>` name, an ownership marker, and `O_NOFOLLOW`. Overlapping suffixes conflict. User files are not overwritten. Teardown is idempotent.
- Linux systemd-resolved plan: dummy link name, an address in 127.0.0.64–127 excluding 127.0.0.1 and 127.0.0.53, route-only `~domain`, `DefaultRoute=no`. Commands are `ip`/`resolvectl` argv tuples. The plan is not executed on this host, including when euid is 0 in the unit test. Live Linux proof is BLOCKED because this machine is macOS.
- Route conflict parsing drops the default route and treats macOS classful `netstat` destinations as networks. A specific route inside the chosen CIDR conflicts.
- Helper lease stores uid, pid, and `ps` start time. The same pid with a different start time is stale and is removed. A live owner is kept.

## AWS resources

Four disposable runs in us-east-1, CIDR `10.70.0.0/16`, tag `stlv-spike=dev-vpc-phase0`. Each `finally` destroyed its own resources. A tag sweep after the last run found no instances, VPCs, `stlv-p0-*` roles or users, self-owned SSM documents, or `stlv-phase0` hosted zones.

| Run | What happened | Destroyed |
| --- | --- | --- |
| 1 | `t4g.nano` bastion. `dnf install -y python3.11` was OOM-killed. Root `AssumeRole` returned AccessDenied. | Yes |
| 2 | `t4g.small` bastion. Bootstrap succeeded. `ssh -f` with captured pipes hung 90s (session-manager plugin filled the stderr pipe). | Yes |
| 3 | SSH, ControlMaster, TCP banners, EIC, and strict host key succeeded. Zone `stlv-phase0.test` stayed NXDOMAIN. | Yes |
| 4 | Same transport result for `stlv-phase0.internal`, including remote Python 3.11.16. Private zone still NXDOMAIN on both resolver addresses. | Yes |

No DocumentDB cluster was created. Listeners were TCP echo on port 8080, not DocumentDB.

## Design revision before Phase 2

1. The privileged helper must own the packet filter. It installs only validated destination CIDRs from the manifest. It must not sudo a user-writable sshuttle, must not grant `NOPASSWD` with a wildcard, and must not put `PYTHONPATH` in sudoers.
2. Split DNS stays a separate loopback relay. `sshuttle --dns` is not the implementation.
3. Re-prove private hosted zone answers over TCP to `169.254.169.253` before building the DNS integration on that assumption. The TCP channel and the relay behavior are proven. The private answer is not.
4. Bastion bootstrap needs an instance type that can install `python3.11` beside the AL2023 system Python. `t4g.nano` (512 MB) cannot. `t4g.small` can. Do not replace `/usr/bin/python3`.
5. Do not depend on the account root calling `AssumeRole`. A scoped IAM user (or a non-root role) is what the EIC and SSM denies were tested with.
6. Live Linux resolver dispatch and live macOS `/etc/resolver` installation still need a host where they can actually be applied. Unit tests are not that proof.
