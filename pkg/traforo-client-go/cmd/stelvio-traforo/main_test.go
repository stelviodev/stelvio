package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestVersionOutput(t *testing.T) {
	previousVersion, previousCommit := version, commit
	t.Cleanup(func() {
		version, commit = previousVersion, previousCommit
	})

	for _, tc := range []struct {
		name, version, commit, want string
	}{
		{name: "release semver", version: "v1.2.3", commit: "abcdef123456", want: "v1.2.3\n"},
		{name: "development commit", version: "dev", commit: "abcdef123456", want: "abcdef123456\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			version, commit = tc.version, tc.commit
			var stdout, stderr bytes.Buffer
			if got := execute([]string{"version"}, &stdout, &stderr); got != 0 {
				t.Fatalf("exit status = %d", got)
			}
			if stdout.String() != tc.want || stderr.Len() != 0 {
				t.Fatalf("stdout=%q stderr=%q want stdout=%q", stdout.String(), stderr.String(), tc.want)
			}
		})
	}
}

func TestInvalidCommandShapesShowUsage(t *testing.T) {
	for _, args := range [][]string{
		{},
		{"helper"},
		{"forwarder"},
		{"forwarder", "3", "extra"},
		{"tunnel", "start"},
	} {
		t.Run(strings.Join(args, "/"), func(t *testing.T) {
			var stdout, stderr bytes.Buffer
			if got := execute(args, &stdout, &stderr); got != 2 {
				t.Fatalf("exit status = %d, want 2", got)
			}
			if !strings.Contains(stderr.String(), "helper serve/install/uninstall") || !strings.Contains(stderr.String(), "forwarder <carrier-fd>") {
				t.Fatalf("usage does not describe the command set: %q", stderr.String())
			}
		})
	}
}

func TestForwarderCommandDelegatesCarrierDescriptor(t *testing.T) {
	var stdout, stderr bytes.Buffer
	called := ""
	status := executeWithForwarder([]string{"forwarder", "8"}, &stdout, &stderr, func(fd string) error {
		called = fd
		return nil
	})
	if status != 0 || called != "8" || stdout.Len() != 0 || stderr.Len() != 0 {
		t.Fatalf("status=%d called=%q stdout=%q stderr=%q", status, called, stdout.String(), stderr.String())
	}
}

func TestUnknownHelperOperationIsRejected(t *testing.T) {
	var stdout, stderr bytes.Buffer
	if got := execute([]string{"helper", "not-an-operation"}, &stdout, &stderr); got != 1 {
		t.Fatalf("exit status = %d, want 1", got)
	}
	if !strings.Contains(stderr.String(), "unknown helper operation") {
		t.Fatalf("error does not report rejected operation: %q", stderr.String())
	}
}
