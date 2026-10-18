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

Install GoReleaser v2.18.3 (the version pinned in `.github/workflows/release.yml`)
and Go compatible with `go.mod`. The script uses `.goreleaser.yml` and the same
cgo/SDK flags, linker flags, and artifact names as CI. It builds both macOS
architectures, validates them, then installs this Mac's image using the normal
privileged replacement transaction. On Apple Silicon, Rosetta must be available
for amd64 command validation. On Intel, arm64 receives structural and embedded
version/metadata validation; native arm64 execution is recorded as pending. Stop managed sessions first. No release needs to be published.
Run as your ordinary user; authorize replacement through the macOS prompt.

Copy the exact `export STLV_TRAFORO_DEV_BINARY=...` command printed by the script
into every shell that will run `stlv`. The variable selects the local build for
helper installation and the isolated forwarder. Build output and provenance JSON
are under the ignored `build/` directory. Re-run the script after Go edits.

`traforo version` reports one semantic version for the whole binary. GoReleaser
injects the release tag and commit through linker flags. Local builds report
`v0.0.7-dev+g<short-commit>`, using the pinned base version rather than Stelvio's
tags. Provenance also records the source fingerprint, including uncommitted edits.
Stelvio requires the exact pinned base version; snapshots require the override.
Selected images are version-checked before installation or host changes.

To build and validate without installing, run `make build` in this directory, or
`uv run python pkg/traforo-client-go/scripts/build.py --build-only` at the repository
root. Both artifacts (`traforo-darwin-arm64`, `traforo-darwin-amd64`) and `build.json`
are published together in a unique ignored `build/traforo-*/` directory. A failed
build preserves prior bundles. The snapshot pipeline disables publication and
receives no GitHub token. Identical source/settings exercise the same build path;
byte-for-byte parity additionally requires matching SDK/toolchain/build metadata.
To restore the pinned release and normal managed networking:

```bash
unset STLV_TRAFORO_DEV_BINARY
uv run stlv tunnel install
```

There is no persistent developer mode or root-owned override marker. The script
sets the variable only inside its installation process; it cannot export into your
shell. Invalid paths, unsafe permissions/ACLs, and incompatible images fail without
falling back.

See `stelvio/tunnel/testing.md` for release, developer, host and AWS acceptance.

## DNS and control ownership

The nonroot forwarder owns one scoped UDP/TCP DNS listener pair per VPC. It sends
upstream TCP DNS through that VPC's numeric SOCKS destination. Listener lifetime
outlasts packet attempts: reconnection keeps the port and rejects owned queries
with SERVFAIL until the new revision is active. The root helper only owns host
resolver files, routes and devices. Python supplies AWS-discovered policy and
coordinates readiness through bounded control-v2 JSON request/reply messages.

Binary compatibility uses the semantic version, while internal helper, carrier,
control and journal format identifiers remain unchanged. DNS control has
configure/reject/retire, upstream/direct-host probes, and arm/check/release query
observations. Request IDs, unit generations, attempts and DNS revisions fence
stale messages. Python owns AWS discovery and orchestration; Go owns DNS protocol handling.

DNS wire support uses pinned `github.com/miekg/dns`. Its Go TLS/X.509 dependency
links Apple's Security framework, explicitly allowed by the developer builder;
upstream DNS transport remains TCP through SOCKS, not DNS over TLS. All linked
libraries are Apple system libraries and the existing 16 MiB size bound remains.

```bash
go test -mod=readonly ./internal/dnsrelay -run='^$' -fuzz='^FuzzAnswer$' -fuzztime=5s -parallel=2
go test -mod=readonly ./internal/forwarder -run='^$' -fuzz='^FuzzControl$' -fuzztime=5s -parallel=2
```

### Native ARM64 race testing

Run `make race` directly on arm64 macOS, including the large-stream regression.
Unit/race results validate local source. Downloaded release validation and native
networking acceptance are separate gates described in the testing guide.
