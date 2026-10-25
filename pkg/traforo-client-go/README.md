# Traforo Go module

This module contains Traforo's bounded protocol, native system adapters, and
privileged helper. The protocol package is independent of operating-system APIs.
The Darwin package contains narrow cgo bindings for native identity, descriptor,
route, and network configuration operations. The privileged helper implementations
live in `internal/helper/darwin` and `internal/helper/linux`; `internal/helper`
provides a portable command dispatcher. The helper authorizes sessions, owns raw
utun or TUN descriptors, and journals route and DNS configuration changes so it
can recover them after interruption.
The module uses the Apache License 2.0. Its code of conduct is maintained in the
main Stelvio repository.

Stelvio pins the Traforo `v0.0.7` release for macOS `arm64` and `x86_64`. It checks
the artifact digest, Mach-O architecture, executable metadata, and reported version
before it uses the image. The AWS Session Manager plugin is pinned at `1.2.835.0`
and uses a separate private cache.

## Local builds

The Darwin GoReleaser configuration builds Darwin `arm64` and `amd64` images
with cgo and a macOS 15 minimum, and cross-builds pure Go Linux images.
The Linux configuration builds Linux `arm64` and `amd64` images without cgo.
The GoReleaser version and release Go toolchain are pinned in the module release
workflow. Stelvio's asset pin remains the source of the local snapshot base version.

The `make build` target is for this module inside Stelvio's monorepo, where the
builder can read Stelvio's release pin and use its existing helper installation
transaction. A standalone split checkout contains only the Go module and cannot
run that target; use a Stelvio checkout for local development builds. The target
checks for the monorepo files and prints this requirement if they are absent.

From the Stelvio checkout, install the pinned Go and GoReleaser versions, then
build and validate the platform's images:

```bash
cd pkg/traforo-client-go
make build
```

This target is build-only. It checks GoReleaser metadata, native executable formats,
the injected snapshot version, and Darwin linked system libraries. It writes an atomic
bundle with `build.json` provenance under `build/` and prints the host image's
`STLV_TRAFORO_DEV_BINARY` export. It does not install the helper or publish a release.

To build and install the host image through the existing authorized helper
transaction, run this from the Stelvio checkout root:

```bash
uv run python pkg/traforo-client-go/scripts/build.py
```

The builder captures and fingerprints source and settings before compilation. It
refuses source changes during the build and preserves prior bundles when a build or
validation fails. On Apple Silicon, it executes the Intel image through Rosetta
when available. On Intel, native arm64 execution remains pending until Apple
Silicon validation is available.

For local Traforo development, set `STLV_TRAFORO_DEV_BINARY` to an absolute path to
the executable. Stelvio accepts `v0.0.7` or `v0.0.7-dev+g<hex>` only through this
explicit override. It validates ownership, permissions, ACLs, architecture, and the
reported version. Invalid overrides fail without falling back to the release or
changing its cache. This selection does not build or install the executable.

`internal/dnsrelay` provides an isolated DNS view for one VPC. It listens only on
loopback over UDP and TCP, serves configured exact hostnames and suffix domains,
and sends upstream DNS over TCP through that view's SOCKS proxy. The resolver
is separate from the selected resource subnet ranges and needs no host route.
Resource readiness checks every A record against those ranges; direct resource
probes still test ordinary host routing. Each view keeps
its listener ports across configuration revisions and rejects owned names with
SERVFAIL while its upstream is unavailable. The relay bounds concurrent requests,
message sizes, connection query counts, and I/O deadlines; it does not cache answers
or modify host resolver settings.

The executable builds on all Go-supported platforms and supports `version`.
Helper operations (`helper serve`, `helper install`, and `helper uninstall`) and
the internal `forwarder <carrier-fd>` operation are available on Darwin and Linux.
Debian 13 with systemd and systemd-resolved is the first supported Linux host;
other operating systems return an unsupported-platform error. The forwarder refuses
root and setuid execution. It creates one userspace IPv4/TCP stack per VPC,
validates carrier generations and destination CIDRs, and opens TCP destinations
through that VPC's loopback SOCKS proxy. UDP forwarding is rejected. Each unit
uses bounded packet and stream queues, and shutdown closes its active streams
and stack before returning. The helper retains raw kernel device descriptors and all
host networking state. Stelvio provides `stlv tunnel install`, `inspect`, `cleanup`,
and `uninstall` for helper administration. Local builds use the verified
`STLV_TRAFORO_DEV_BINARY` selection; they do not alter release pins or cache entries.

Packet units retain 1–32 selected resource subnet ranges without expanding them
to whole VPC blocks. The same ranges gate inbound packets, return packets, SOCKS
resource connections, and DNS resource readiness. The VPC resolver uses SOCKS
separately and needs no host route.

The forwarder control interface uses bounded newline-delimited JSON version 2
messages with strictly increasing request IDs. It supports unit add/remove and
DNS configure/reject/retire/probe/observation operations. Unit generations,
transport attempts, and DNS revisions fence stale operations. Re-adding a unit
requires a higher helper generation, which also drops delayed carrier frames
from its previous attempt. DNS probes run outside packet-manager locks. The
helper does not accept nonprivileged requests to install or remove arbitrary
host resources. It changes only resources that its durable receipt and journal
can certify as owned, and retains recovery evidence when ownership is uncertain.

The protocol package and executable can be tested independently on supported Go
hosts with `go test ./internal/protocol ./cmd/stelvio-traforo`. Forwarder and
helper runtime tests use Unix sockets. Darwin helper tests also require cgo, the
Apple SDK, and Clang. Supported runtimes are macOS 15 or newer on `arm64` and
`amd64`, and Debian 13 with systemd-resolved. Native Linux validation uses ARM64;
AMD64 currently receives build validation.

Run checks from this directory:

```bash
make test
make race
make vet
make lint
make fuzz
make fuzz-snapshot
make fuzz-dns
make fuzz-control
```

Run `make format` to apply the configured Go formatters. The Darwin tests compile
small native boundary fixtures and use temporary files, sockets, and processes.
Forwarder tests use userspace stacks and loopback SOCKS fixtures; they do not
require privileged routes. The tests do not invoke helper installation or
uninstallation and do not change host routes or DNS settings. Live installation
and host recovery acceptance require a later, separately authorized test step.

The independent vectors in `internal/helper/testdata/independent_vectors.json`
are shared test data. Keep their encoded bytes stable so Go and Python tests can
check the same protocol and journal examples independently.

## Debian 13 development

See `scripts/debian/README.md` in the Stelvio monorepo for the Lima environment.
The Linux helper lives in `internal/helper/linux`. It owns nonpersistent TUN
interfaces, rtnetlink routes, separate dummy interfaces for per-VPC DNS views,
and a Linux-specific durable journal. The forwarder and DNS relay remain ordinary
user processes. Peer credentials, pidfds, process birth ticks, and boot identity
fence the owner and its foreground parent. Raw TUN descriptors never leave the
helper. Ambiguous resource ownership retains recovery evidence.

The systemd service is `stelvio-traforo.service`. Its image is installed at
`/usr/local/libexec/stelvio-traforo`, with durable state in
`/var/lib/stelvio/tunnel` and a runtime socket in `/run/stelvio/tunnel`.
Resolved integration uses its D-Bus API, route-only domains, and explicit relay
ports. It does not replace `/etc/resolv.conf` or the uplink's DNS settings.

Linux snapshots use `.goreleaser-linux.yml`, build ARM64 and AMD64 without cgo,
and validate ELF structure, version, digest, and build provenance. ARM64 runtime
acceptance is validated in Debian; native AMD64 acceptance is separate. The
Darwin release configuration also cross-builds the Linux images. Linux Traforo
release downloads remain unavailable until a release is published and pinned;
use `STLV_TRAFORO_DEV_BINARY` for local development. The pinned Linux Session
Manager plugin is verified from its official versioned Debian package without
running its installer.

Go tests, race checks, vet, lint, and the bounded fuzz targets run on Linux.
Privileged host tests are explicitly selected with
`--integration-linux-privileged -n 0`; ordinary test runs cannot enable them.
