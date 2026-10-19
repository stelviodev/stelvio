# Contributing to Traforo Go

Keep protocol validation bounded and independent of Darwin APIs. Keep native
system calls behind the Darwin adapter; authorization and lifecycle policy belong
in higher-level packages.

Use the shared vectors in `internal/helper/testdata/independent_vectors.json` as
independent test data. Do not regenerate expected bytes with the encoder under
test. Preserve the vector format for later Go and Python consumers.

From this directory, run `make test`, `make race`, `make vet`, and `make lint`.
Run `make fuzz` for the bounded request decoder. Run `make format` before you
commit Go source changes. Darwin tests require macOS, cgo, and the Apple SDK.
Tests must not install a service or mutate host networking.
