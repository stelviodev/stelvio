# Traforo Go module

This module contains Traforo's bounded protocol, Darwin system adapter, and
privileged helper. The protocol package is independent of operating-system APIs.
The Darwin package contains narrow cgo bindings for native identity, descriptor,
route, and network configuration operations. The privileged helper implementation
lives in `internal/helper/darwin`; `internal/helper` provides a portable command
dispatcher. The helper authorizes sessions, owns raw utun descriptors, and journals
route and scoped resolver changes so it can recover them after interruption.

Stelvio pins the Traforo `v0.0.7` release for macOS `arm64` and `x86_64`. It checks
the artifact digest, Mach-O architecture, executable metadata, and reported version
before it uses the image. The AWS Session Manager plugin is pinned at `1.2.835.0`
and uses a separate private cache.

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
the internal `forwarder <carrier-fd>` operation are available only on Darwin;
other platforms return an unsupported-platform error. The forwarder refuses
root and setuid execution. It creates one userspace IPv4/TCP stack per VPC,
validates carrier generations and destination CIDRs, and opens TCP destinations
through that VPC's loopback SOCKS proxy. UDP forwarding is rejected. Each unit
uses bounded packet and stream queues, and shutdown closes its active streams
and stack before returning. The helper retains raw utun descriptors and all
host networking state. Stelvio does not expose a Python client or a tunnel
command in this step.

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
helper runtime tests use Darwin Unix sockets; helper tests also require cgo, the
Apple SDK, and Clang. Runtime support is limited to macOS 15 or newer on
`arm64` and `amd64`.

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
