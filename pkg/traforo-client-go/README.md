# Traforo Go module

This module contains Traforo's bounded protocol and its Darwin system adapter.
The protocol package is independent of operating-system APIs. The Darwin package
contains narrow cgo bindings for native identity, descriptor, route, and network
configuration operations. This step does not include an executable or installable
service.

The protocol tests can run on any supported Go host. The Darwin package requires
macOS, cgo, the Apple SDK, and Clang. Runtime support is limited to macOS 15 or
newer on `arm64` and `amd64`.

Run checks from this directory:

```bash
make test
make race
make vet
make lint
make fuzz
```

Run `make format` to apply the configured Go formatters. The Darwin tests compile
small native boundary fixtures and use temporary files, sockets, and processes.
They do not install a service or change host routes or DNS settings.

The independent vectors in `internal/helper/testdata/independent_vectors.json`
are shared test data. Keep their encoded bytes stable so Go and Python tests can
check the same protocol examples independently.
