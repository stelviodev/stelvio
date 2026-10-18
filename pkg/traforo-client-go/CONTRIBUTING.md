# Contributing to Traforo

Contribution guidelines: <https://github.com/stelviodev/stelvio/blob/main/CONTRIBUTING.md>.

The Stelvio monorepo source is authoritative for the Go client API and unit tests.
The split repository owns release builds and Go implementation CI.

Run `make lint vet test race` on macOS. Run race tests natively on arm64 as well
as Intel where available. Use the shared GoReleaser builder and
`STLV_TRAFORO_DEV_BINARY` workflow in the README for local integration testing.
Python tests do not compile Go. Host networking and AWS acceptance are separate,
explicitly enabled test tiers described in Stelvio's tunnel testing guide.
