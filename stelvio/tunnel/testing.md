# Traforo testing

Python tests validate helper requests, reply framing, descriptor handling,
installation transactions, and forwarder control messages. They do not compile Go.
The Go module owns the forwarder and helper implementations.

## Unit tests

Run the Python tunnel suite with the normal repository checks:

```bash
uv run pytest tests/tunnel -q
```

Host-mutating tests use the `integration_macos_privileged` marker. They skip in
ordinary runs and in `tests/integration/run_all.sh`. Tests that need a binary use
the checksum-verified image selected by the asset module.

## Unprivileged forwarder smoke test

The optional smoke test starts the installed, verified Traforo image as the current
nonroot user. It uses loopback SOCKS and DNS fixtures and a datagram socket pair.
It does not contact the helper, install software, or change host routes or resolvers.

```bash
STLV_TEST_TRAFORO_FORWARDER=1 uv run pytest \
  tests/tunnel/test_forwarding.py -k verified_nonroot -q
```

Run it on macOS after `stlv tunnel install` has installed the image selected by
`STLV_TRAFORO_DEV_BINARY` or the pinned release. The installed image must match the
selected digest. The test never downloads or builds a binary.

## Privileged macOS acceptance

This lane can install or replace the helper and changes routes, interfaces, and
scoped resolver files while it runs. It needs macOS 15 or later, an ordinary user,
administrator authorization, and exclusive access to the host. It must run without
xdist workers and without another integration tier:

```bash
uv run pytest tests/integration/test_tunnel_host.py \
  tests/integration/test_tunnel_dns.py \
  --integration-macos-privileged -n 0 -q
```

The session fixture records the current helper and host baseline, refuses active or
uncertain state, and restores the original helper during teardown. It compares
resolver files, DNS state, routes, interfaces, and networking processes. Stop if
preflight or teardown cannot prove ownership and restoration. Do not delete retained
helper journals or resolver files by hand.

The distinct-candidate upgrade case needs a verified candidate supplied through
`STLV_TRAFORO_DEV_BINARY`; Step 9 supplies the local builder. Cross-venv acceptance
needs a wheel at `STLV_TEST_STELVIO_WHEEL`. Record either check as pending when its
artifact is not available.
