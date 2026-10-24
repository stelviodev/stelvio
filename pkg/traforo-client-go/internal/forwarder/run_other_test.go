//go:build !darwin && !linux

package forwarder

import (
	"strings"
	"testing"
)

func TestRunRejectsForwarderOutsideDarwin(t *testing.T) {
	if err := Run("3"); err == nil || !strings.Contains(err.Error(), "only supported on Darwin") {
		t.Fatalf("Run error = %v", err)
	}
}
