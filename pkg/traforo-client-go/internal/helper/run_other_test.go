//go:build !darwin && !linux

package helper

import (
	"strings"
	"testing"
)

func TestRunRejectsOperationsOutsideDarwin(t *testing.T) {
	for _, operation := range []string{"serve", "install", "uninstall"} {
		t.Run(operation, func(t *testing.T) {
			err := Run(operation)
			if err == nil || !strings.Contains(err.Error(), "only supported on Darwin") {
				t.Fatalf("Run(%q) error = %v", operation, err)
			}
		})
	}
}
