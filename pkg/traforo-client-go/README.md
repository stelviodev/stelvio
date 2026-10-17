# Traforo Go client

This source is authoritative for the Go client API and its unit tests. Changes
under this directory are mirrored to https://github.com/stelviodev/traforo through
the repository split. That repository owns release builds and Go implementation CI.

Normal `stlv` installations download checksum-pinned releases; Go source, developer
scripts and executables are excluded from Stelvio's wheel and sdist.

## Test local changes

From this directory, run Go tests directly, never through pytest:

```bash
go test -mod=readonly ./...
go test -mod=readonly -race ./...
```

## Build and install on this Mac

From the Stelvio repository root, using its development environment:

```bash
uv run python pkg/traforo-client-go/scripts/build.py
```

The script builds the native architecture with cgo and the installed macOS SDK,
validates the image, and installs it using the normal privileged replacement
transaction. Stop managed sessions first. No release needs to be published.
Run as your ordinary user; authorize replacement through the macOS prompt.

Copy the exact `export STLV_TRAFORO_DEV_BINARY=...` command printed by the script
into every shell that will run `stlv`. The variable selects the local build for
helper installation and the isolated forwarder. Build output and provenance JSON
are under the ignored `build/` directory. Re-run the script after Go edits.

Managed networking requires `forwarder=2` and `dns=1`, supplied by the pinned
v0.0.5 release. The override is optional and intended for testing local changes.
Selected images are capability-checked before installation or host changes.
To restore the pinned release and normal managed networking:

```bash
unset STLV_TRAFORO_DEV_BINARY
uv run stlv tunnel install
```

There is no persistent developer mode or root-owned override marker. The script
sets the variable only inside its installation process; it cannot export into your
shell. Invalid paths, unsafe permissions/ACLs, and incompatible images fail without
falling back. Older Stelvio versions do not recognize the override.

See `stelvio/tunnel/testing.md` for release, developer, host and AWS acceptance.

## DNS and control ownership

The nonroot forwarder owns one scoped UDP/TCP DNS listener pair per VPC. It sends
upstream TCP DNS through that VPC's numeric SOCKS destination. Listener lifetime
outlasts packet attempts: reconnection keeps the port and rejects owned queries
with SERVFAIL until the new revision is active. The root helper only owns host
resolver files, routes and devices. Python supplies AWS-discovered policy and
coordinates readiness through bounded control-v2 JSON request/reply messages.

`stelvio-traforo capabilities` returns `{"forwarder":2,"dns":1}`. The existing
`version`, helper protocol and carrier protocol stay unchanged. DNS control has
configure/reject/retire, upstream/direct-host probes, and arm/check/release query
observations. Request IDs, unit generations, attempts and DNS revisions fence
stale messages. No DNS parser or dnspython dependency remains in Python.

DNS wire support uses pinned `github.com/miekg/dns`. Its Go TLS/X.509 dependency
links Apple's Security framework, explicitly allowed by the developer builder;
upstream DNS transport remains TCP through SOCKS, not DNS over TLS. All linked
libraries are Apple system libraries and the existing 16 MiB size bound remains.

```bash
go test -mod=readonly ./internal/dnsrelay -run='^$' -fuzz='^FuzzAnswer$' -fuzztime=5s -parallel=2
go test -mod=readonly ./internal/forwarder -run='^$' -fuzz='^FuzzControl$' -fuzztime=5s -parallel=2
```

Stelvio itself no longer requires `dnspython`. Application drivers such as PyMongo
may independently install it for their own DNS/SRV support; that is separate from
Traforo and the tunnel. A clean Stelvio-only wheel environment should have no
`dns` module.
