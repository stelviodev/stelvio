Contribution guidelines are maintained in the main repository: https://github.com/stelviodev/stelvio/blob/main/CONTRIBUTING.md


### ARM64 race-detector limitation in the pinned TCP stack

The new large-stream regression exposes a defect in the pinned gVisor revision's
`pkg/sync/race_arm64.s`: the CAS assembly loads `new` into R1 instead of R2. TCP
workers spin instead of parking under ARM64 race instrumentation. The regression
fails early with an actionable error on that combination rather than hanging or
silently skipping. Normal native ARM64 tests and networking are separate.

On an arm64 Mac with Rosetta, run the same race suite as amd64:

```sh
GOARCH=amd64 CGO_ENABLED=1 make race
```

Native ARM64 race acceptance remains blocked until a separately reviewed upstream
dependency fix is adopted. Do not patch the module cache or weaken the stream test.
This workaround changes neither release pins nor production architecture selection.
