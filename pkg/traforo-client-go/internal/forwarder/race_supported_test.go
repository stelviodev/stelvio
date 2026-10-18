//go:build !race || !arm64

package forwarder

import "testing"

func checkStreamRacePlatform(t *testing.T) { t.Helper() }
