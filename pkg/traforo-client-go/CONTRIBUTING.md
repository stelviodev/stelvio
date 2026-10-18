Contribution guidelines are maintained in the main repository: https://github.com/stelviodev/stelvio/blob/main/CONTRIBUTING.md


### Native ARM64 race testing

Run `make race` directly on arm64 macOS. The updated gVisor Go-branch revision
`v0.0.0-20261017122056-1b7906fdee8c` fixes the CAS register defect in
`pkg/sync/race_arm64.s`; the large-stream regression no longer has a platform guard.
The previous pin required amd64/Rosetta for race testing. That limitation remains
historical evidence in the tunnel testing guide, not a current testing prerequisite.
Dependency changes do not update the pinned downloadable Traforo release or the
installed helper. Native networking acceptance is separate from these unit tests.
