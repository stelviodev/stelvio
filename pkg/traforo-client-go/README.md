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

Without the variable, managed startup selects the pinned release and replaces a
local installation when safe. To return explicitly:

```bash
unset STLV_TRAFORO_DEV_BINARY
uv run stlv tunnel install
```

There is no persistent developer mode or root-owned override marker. The script
sets the variable only inside its installation process; it cannot export into your
shell. Invalid paths, unsafe permissions/ACLs, and incompatible images fail without
falling back. Older Stelvio versions do not recognize the override.

See `stelvio/tunnel/testing.md` for release, developer, host and AWS acceptance.
