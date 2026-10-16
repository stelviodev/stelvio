//go:build darwin

package darwin

import (
	"os/exec"
	"path/filepath"
	"testing"
)

func TestNativeSyscallBoundaries(t *testing.T) {
	for _, kind := range []string{"host_routes", "route_reply"} {
		t.Run(kind, func(t *testing.T) {
			binary := filepath.Join(t.TempDir(), "check")
			cmd := exec.Command("/usr/bin/clang", "-std=c17", "-Wall", "-Wextra", "-Werror", "-I.", "bindings_darwin.c", "testdata/check_"+kind+".c", "-lbsm", "-framework", "SystemConfiguration", "-framework", "CoreFoundation", "-o", binary)
			if out, e := cmd.CombinedOutput(); e != nil {
				t.Fatalf("compile: %v %s", e, out)
			}
			scenarios := map[string]int{"owned": 0, "growth": 0, "growth-foreign": 1, "growth-malformed": 2, "always-growth": 2, "oversized": 2, "permission": 2}
			if kind == "route_reply" {
				scenarios = map[string]int{"success": 0, "notifications": 0, "foreign": 0, "length": 1, "version": 1, "error": 1, "short-add": 1, "truncated": 1, "foreign-only": 1, "sequence-only": 1, "missing": 1}
			}
			for scenario, want := range scenarios {
				t.Run(scenario, func(t *testing.T) {
					out, e := exec.Command(binary, scenario).CombinedOutput()
					status := 0
					if e != nil {
						exit, ok := e.(*exec.ExitError)
						if !ok {
							t.Fatal(e)
						}
						status = exit.ExitCode()
					}
					if status != want || len(out) != 0 {
						t.Fatalf("status %d want %d: %s", status, want, out)
					}
				})
			}
		})
	}
}
