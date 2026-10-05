# P0 native forwarding proof

This is new implementation work for `tasks/dev-vpc.md`, independent of the
`feature/documentdb-vpc` prototype. It is **not** the finished tunnel feature.
The first proof isolates native packaging, privilege separation, IPv4 TCP
forwarding, two destination ranges, and ordinary teardown. It creates no AWS
resources and changes no DNS settings. SSH/SSM, DocumentDB, OS DNS, temporary
AWS ownership and the product installer are later P0
proofs. G0 remains open until all of those are proven.

## Artifact and privilege boundary

- `native/broker/main.c`: compiled native helper, system libSystem only. Creates
  a kernel-assigned utun, checks existing host routes for overlaps, configures
  only canonical private IPv4 `/16` through `/28` ranges, and passes a descriptor
  to the sudo caller's authenticated local UID. Root handles no TCP, AWS,
  credentials, executable paths, project code, or Python runtime.
- `native/main.go`: developer-owned forwarding process. gVisor TCP via the
  pinned tun2socks core, explicit Darwin family framing, destination-based
  selection of loopback SOCKS listeners. UDP is closed. Bounded connections,
  handshake and half-close waits. The broker must authenticate as root.
- `build/broker` and `build/forwarder`: independent executables; neither needs
  the building/launching venv. `build/SHA256SUMS` and
  `build/runtime-libraries.txt` identify this exact build.

Compiled on macOS 15.7.5 arm64 using Apple clang and Go 1.25.3. This is a tested
build profile, not a live support claim. `native/go.mod` and `native/go.sum`
pin dependency versions/checksums. No code under `stelvio/` has been migrated.

## First manual proof (no AWS)

Run from the repository root. Leave other terminal sessions open. Root commands
are explicit native system commands; do not run the forwarder or a project
interpreter as root.

1. Build and inspect the read-only preflight:

   ```sh
   sh spikes/dev-vpc-v1/build.sh
   spikes/dev-vpc-v1/build/broker --check 10.254.0.0/16 10.253.0.0/16
   cat spikes/dev-vpc-v1/build/runtime-libraries.txt
   ```

   Expected: `host route preflight PASS (no changes made)`. Conflicting host,
   LAN or VPN routes cause refusal. Do not bypass a conflict. The broker checks
   again before changing routes; a concurrent external network change remains
   a limitation of this P0 proof.

2. Install the dedicated proof binary. This creates **one** system file:
   `/Library/PrivilegedHelperTools/dev.stelvio.vpc-proof`, root:wheel, mode 0755.
   The existing `/Library/PrivilegedHelperTools` directory must be root-owned
   and not group/world writable (the broker verifies this). No launchd service,
   sudoers entry, Python runtime or AWS deployment is installed.

   First check for a prior artifact:

   ```sh
   ls -l /Library/PrivilegedHelperTools/dev.stelvio.vpc-proof
   ```

   If present, stop here and report it; do not overwrite an existing artifact.
   If absent:

   ```sh
   sudo /usr/bin/install -o root -g wheel -m 0755 spikes/dev-vpc-v1/build/broker /Library/PrivilegedHelperTools/dev.stelvio.vpc-proof
   /Library/PrivilegedHelperTools/dev.stelvio.vpc-proof --version
   shasum -a 256 /Library/PrivilegedHelperTools/dev.stelvio.vpc-proof spikes/dev-vpc-v1/build/broker
   ```

   Both hashes must agree. Executing the project copy with `sudo` is deliberately
   refused by the broker's installed-path and root-ownership checks.

3. Terminal A, launch the forwarder as your normal user:

   ```sh
   spikes/dev-vpc-v1/build/forwarder --local
   ```

   It prints the broker command and waits up to 90 seconds. In terminal B run:

   ```sh
   sudo /Library/PrivilegedHelperTools/dev.stelvio.vpc-proof --serve 10.254.0.0/16 10.253.0.0/16
   ```

   This creates `/private/var/run/stelvio-vpc-proof/` (root-owned), a locked
   lease, a journal and one caller-owned Unix socket; one temporary utun and two
   routes are installed after the forwarding stack acknowledges readiness.
   Expected in A: `READY uid=<your uid> interface=utun<N> vpcs=2`.

4. Terminal C, exercise ordinary OS sockets against the two routed addresses:

   ```sh
   .venv/bin/python spikes/dev-vpc-v1/probe.py
   ```

   Expected: two `PASS` results, `vpc-1` at `10.254.10.10` and `vpc-2` at
   `10.253.20.20`, each with 1,048,576 exact binary payload bytes and EOF after
   half-close. The fake SOCKS endpoints each reject the other VPC's addresses.
   This proves the OS socket → utun → selected SOCKS path only, not AWS access.

5. Press Ctrl+C in A. B should exit, deleting both routes, its socket and its
   journal. Record A's interface name and verify it is gone:

   ```sh
   /usr/sbin/netstat -rn -f inet
   sudo /bin/ls -la /private/var/run/stelvio-vpc-proof
   ```

   There must be no proof routes, `broker.sock` or `journal`; the inactive lease
   file and directory remain. If any cleanup fails, **retain the journal** and
   report the output. Do not remove a journal or route to force a passing result.
   Use the dedicated installed helper's `--reconcile` only after the session
   has exited. It refuses malformed journals or surviving/reused interfaces.

6. If the normal proof passed, repeat steps 3–5 once from a different working
   directory/venv using the same installed helper and absolute forwarder path.
   Its runtime origin must remain independent of the first venv. Full deleted-
   installer-venv acceptance through the product CLI remains a later gate.

Send back the build/preflight output, A/B/C results, and teardown listing. For
removal, use `sudo /Library/PrivilegedHelperTools/dev.stelvio.vpc-proof --uninstall`.
It refuses an active proof, reconciles safe stale state, then removes the artifact
and empty local state. Do not manually remove journals to force a pass. This P0
assumes no concurrent installation/replacement: its inode lock cannot identify
a process loaded before a reinstall. Production installer generation validation
and atomic lifecycle behavior remain open.

## Local checks already run

- Existing VPC/Function/bridge baseline: 158 passed.
- Native C compiler: `-std=c17 -Wall -Wextra -Werror` passed.
- `go vet .` passed.
- One Go behavioral test with race detection passed: simultaneous independent
  VPC destinations, nonperiodic binary megabyte payloads and client-first FIN.
  It uses bounded packet queues instead of utun. A macOS Unix datagram socket
  simulator was rejected because burst writes return ENOBUFS; retrying that
  simulator did not make it a reliable packet device.
- The Python live probe passes Ruff formatting/lint checks. The first actual
  macOS utun run passed both 1 MiB destination checks and normal cleanup on
  2026-10-05; concrete identities and evidence are in `tasks/dev-vpc.md`.

The user subsequently authorized the implementation agent to run the no-AWS
proof. Both original forwarding/normal cleanup and the subsequent lifecycle
proof passed. The second run launched the forwarder outside the project, passed
both payload checks and active uninstall refusal, then deliberately crashed
the broker. Descriptor EOF terminated the forwarder and kernel routes/interface
disappeared; the retained journal/socket were reconciled. Full dedicated helper
uninstall passed: **no proof helper or local state remains installed**. Concrete
identities/hashes are in `tasks/dev-vpc.md`. Deleted-installer-venv/product CLI
acceptance is still open. No AWS or DNS mutations were run.

The first user-run AWS proof is in [AWS-OWNERSHIP.md](AWS-OWNERSHIP.md). It covers
two customized VPC fixtures and separate temporary ingress ownership; verified
SSH/SSM and OS private DNS remain separate required proofs.
