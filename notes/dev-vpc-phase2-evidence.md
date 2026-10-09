# Dev VPC Phase 2 evidence

Date: 2 October 2026. Host: macOS darwin 24.6.0. Python 3.12.11. dnspython 2.8.0. Helper protocol version 1, helper version `1`. sshuttle is not packaged and is not a candidate.

Phase 2 implemented the revised transport from the plan revision: a versioned helper, a non-root TCP proxy, and a session-owned DNS relay. It did not implement `stlv dev --network`, the invocation queue, stub deadlines, public docs, or the changelog. No AWS resources were created. `spikes/dev-vpc/` was left in place.

## What was proven locally

`uv run ruff check stelvio/dev stelvio/cli/tunnel.py stelvio/cli/__init__.py tests/dev` passed.
`uv run pytest tests/dev -q` passed: 71 tests.

Those tests cover manifest validation against the Phase 1 metadata shape, supervisor state transitions, reconnect classification and the 1s–30s backoff inside a 2-minute window, DNS allowlist / REFUSE / SERVFAIL / UDP truncation, CIDR conflict detection, helper rejection of arbitrary command, path, sudoers, hosts rewrite, and sshuttle tokens, lease identity including PID reuse, rollback of a partial setup plan, and pf rule text for validated CIDRs only. A non-privileged DNS relay bound a high port on 127.0.0.1 and forwarded an allowlisted name over TCP to a local stand-in. Ed25519 keys were generated with `ssh-keygen` into a mode-0700 directory and a mode-0600 file. The client policy tests do not call AWS.

The supervisor calls the helper. Startup sends `setup`, update and reconnect send `update`, and shutdown sends `teardown`. A failed setup rolls back inside the helper, leaves no lease, and does not become READY. Teardown is idempotent. Tests inject the helper client and a fake applicator. They do not run `pfctl` or write `/etc/resolver`.

`sudo -n true` on this Mac returned `sudo: a password is required` (exit 1). This pass did not retry elevation. Live `/etc/resolver` files and live pf anchors were not installed. The helper writes rule text and, when tests give it a temporary directory, resolver files there. `applied` is true only when the injected platform applicator returns true. The default applicator returns false and does not call `pfctl`. A test double that returns true is not a live packet filter. `pfctl` is not executed. The Linux adapter records an `ip`/`resolvectl` plan with `executed` false even when that test double returns true. This host did not run that plan.

## What is not proven

Private hosted-zone answers are not proven. Phase 0 saw NXDOMAIN for an associated private zone. `PRIVATE_HOSTED_ZONE_ANSWERS_PROVEN` in `stelvio/dev/dns.py` is `False`. The relay tests use a scripted upstream. They do not pretend a private zone answered.

Live Linux systemd-resolved was not run. Linux `SO_ORIGINAL_DST` was parsed from a fake socket result only.

macOS `lookup` is unit-tested with `os.open` and `fcntl.ioctl` replaced, so `/dev/pf` was not opened. A failed lookup closes the proxy socket and does not invent a target. Combined with pf not being applied, the redirect-plus-proxy path is not a live proof. A01 stays unmet.

Phase 3 starts and stops `TunnelSupervisor` and reads `StatusBus`. The supervisor and helper are wired through the injected client. They are not yet the dev-session process tree.

`default_elevate` runs `sudo -n --` with a fixed argument array when the process is not root. If it is already root it returns 0 without copying files. Tests never invoke sudo.

## Helper protocol

Operations: `setup`, `update`, `teardown`, and `lookup`. Handshake: `protocol_version` must be `1` and `helper_version` must be `"1"`. Any other field name is rejected before the operation allowlist is applied.

Rejected field names: `command`, `cmd`, `exec`, `executable`, `path`, `shell`, `sudo`, `sudoers`, `pythonpath`, `hosts`, `etc_hosts`, `sshuttle`, `method`, `firewall`, `argv`, `script`, `routes`, `ssh_cmd`, `rewrite_hosts`. String values that contain `NOPASSWD`, `/etc/hosts`, `PYTHONPATH`, `sshuttle`, or `SETENV` are rejected. The helper does not accept a shell string, an executable path, client-supplied routes, or an `/etc/hosts` rewrite.

`setup` and `update` accept only: `op`, `protocol_version`, `helper_version`, `nonce`, `uid`, `pid`, `start_identity`, `tcp_cidrs`, `dns_suffixes`, `dns_port`, `proxy_port`, `platform`. CIDRs are re-validated: IPv4, prefix at least /16, no overlap with loopback, link-local, multicast, or `0.0.0.0/8`. The default route is not installed. Ports must be unprivileged and distinct. The platform must match the helper.

`teardown` with a nonce accepts only the identity fields. `nonce: null` is stale recovery: it removes marker-owned state and does not tear down a live session. Identity is uid plus process start token, not pid alone. A reused pid with a different start token is stale. Resolver files without the Stelvio ownership marker are left alone. A second live session is refused before routes are changed. Parent death is a pipe: EOF removes that lease. A failed install after resolver files exist deletes those files and writes no lease. A failed update restores the previous rules and leaves the previous lease in place; the supervisor then tears that lease down and leaves READY.

`lookup` accepts only `op`, `protocol_version`, `helper_version`, `nonce`, `local_address`, `local_port`, `remote_address`, and `remote_port`. It requires a live lease owned by the socket peer. The success response is only `ok`, `address`, and `port`. Failure is `lookup_failed` and does not return a guessed address. The same rejected field names and smuggled shell tokens apply.

Sudoers, when an elevated install copies it, names the fixed helper path, disables setenv for that command, and has no wildcard. Elevation is `["sudo", "-n", "--", <helper path>]`.

## Proxy and helper split

The helper is the only component allowed to own packet-filter text and resolver files. It redirects only validated destination CIDRs to `127.0.0.1` on the session proxy port. It does not redirect `0.0.0.0/0`, loopback, link-local, or multicast, so AWS API and SSM traffic stays on the host default route.

The TCP proxy runs as the session user, binds `127.0.0.1` only, and opens channels through an injected opener. The SSH policy for that opener is one OpenSSH ControlMaster over SSM `AWS-StartSSHSession`: strict host-key checking, batch mode, identities only, no agent forwarding, no PTY, a private control socket, `ServerAliveInterval` 15, `ServerAliveCountMax` 3, and failure if a forward cannot be set up. The ephemeral Ed25519 key stays in the session directory. It is not placed in the environment, logs, or Pulumi outputs. EIC publish is hard-coded to OS user `stlv-tunnel`. Host-key bootstrap returns only the instance id and the Phase 1 document name. `AWS-RunShellScript` and `AWS-RunPowerShellScript` are refused.

DNS is a separate loopback relay (UDP and TCP). Names outside the suffix allowlist are REFUSED. When the tunnel is down the relay returns SERVFAIL for private suffixes and does not fall back to public DNS. The upstream must be a loopback address, which is the SSH local forward to `169.254.169.253:53`, not a direct connect to the VPC resolver. macOS resolver files and the Linux dummy-link plan are generated behind that boundary and were not installed on the live host.

## Acceptance against the Phase 2 exit

The plan's Phase 2 exit asks for A03–A06, A08, A10, and A13 in OS integration tests. That bar is not met. Local unit tests are recorded separately and do not close the live cases.

| Case | Verdict | Why |
| --- | --- | --- |
| A03 | FAIL | Private hosted-zone answers are unproven. The relay unit tests cover allowlist, REFUSE, SERVFAIL, and UDP truncation against a scripted upstream. Live OS resolver and application-client discovery were not run. |
| A04 | BLOCKED | Relay policy unit tests pass, including no public fallback on tunnel loss. Overlapping suffixes are rejected. Live confirmation that public DNS and AWS API/SSM still use the existing resolver requires installed resolver files, which were not installed. |
| A05 | BLOCKED | The supervisor calls the helper on start, update, reconnect, and stop, and parent-death cleanup passed in unit tests. No reload, idle, long handler, or debugger session was run. That integration is Phase 3. |
| A06 | BLOCKED | State transitions, permanent-versus-transient classification, jittered backoff, the two-minute window, and helper teardown on permanent failure passed in unit tests. SSH, SSM, credentials, and workstation networking were not faulted live. |
| A08 | BLOCKED | Lease identity, PID reuse, parent-death, foreign-file preservation, partial-setup rollback, and failed-update restore passed in temporary directories. Live pf and `/etc/resolver` cleanup were not run because `sudo -n` needs a password. |
| A10 | PASS for client policy only | Unit tests pin strict known_hosts, reject a changed key, constrain EIC to `stlv-tunnel`, refuse `AWS-RunShellScript`, and keep the private key out of argv and the environment. No AWS call this phase. Phase 0's live EIC and ingress evidence was not re-run and is not expanded here. EIC refresh on reconnect was not executed. |
| A13 | PASS for preflight logic | Unit tests reject overlapping specific routes (both ranges reported), two used VPCs, a second host session, an incompatible helper version, and broad CIDRs. An unused VPC and a default route alone are ignored. No live route table was modified. Unsupported-OS detection is the platform check; live Linux was not run. |

Private hosted-zone resolution: not PASS. Live Linux: not PASS.

## Whether Phase 3 may start

Phase 3 may start against this runtime: `TunnelSupervisor.start` / `update` / `stop` and `StatusBus` are the seam, and the manifest is the serialized Phase 1 metadata. Phase 3 must not treat the Phase 2 OS-integration exit as met. Still open: private hosted-zone answers, live pf, live `/etc/resolver`, live Linux, and a real `DIOCNATLOOK` or `SO_ORIGINAL_DST` against the kernel. `rules_applied: false` means the packet filter did not run. A01 and A02 were not part of this phase and stay unmet.
