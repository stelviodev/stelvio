//go:build race && arm64

package forwarder

import "testing"

func checkStreamRacePlatform(t *testing.T) {
	t.Helper()
	// Pinned gVisor pkg/sync/race_arm64.s loads new into R1 instead of R2.
	// Sleeper CAS then fails and nosplit workers spin, starving test deadlines.
	// Fail before starting those workers; do not silently skip this regression.
	t.Fatal("pinned gVisor ARM64 race assembly cannot park TCP workers; on macOS use GOARCH=amd64 CGO_ENABLED=1 make race with Rosetta. Native ARM64 race acceptance requires an upstream dependency fix")
}
