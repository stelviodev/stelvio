//go:build darwin

package helper

import (
	"errors"
	"fmt"
	"io"
	"testing"

	"golang.org/x/sys/unix"
)

func TestResolverReceiptOwnership(t *testing.T) {
	expected := []byte("domain db.internal\n")
	st := unix.Stat_t{Dev: 7, Ino: 11, Mode: unix.S_IFREG | 0644, Nlink: 1}
	receipt := Receipt{Phase: 2, Device: 7, Inode: 11}

	tests := []struct {
		name     string
		data     []byte
		stat     unix.Stat_t
		receipt  Receipt
		prefix   bool
		accepted bool
	}{
		{name: "exact owned resolver", data: expected, stat: st, receipt: receipt, accepted: true},
		{name: "owned staged prefix", data: expected[:8], stat: st, receipt: receipt, prefix: true, accepted: true},
		{name: "prefix required for incomplete file", data: expected[:8], stat: st, receipt: receipt},
		{name: "foreign contents", data: []byte("domain other.internal\n"), stat: st, receipt: receipt},
		{name: "foreign inode", data: expected, stat: st, receipt: Receipt{Phase: 2, Device: 7, Inode: 12}},
		{name: "unexpected hard link", data: expected, stat: func() unix.Stat_t { v := st; v.Nlink = 3; return v }(), receipt: receipt},
		{name: "wrong mode", data: expected, stat: func() unix.Stat_t { v := st; v.Mode = unix.S_IFREG | 0600; return v }(), receipt: receipt},
		{name: "oversized content", data: append(append([]byte(nil), expected...), 'x'), stat: st, receipt: receipt, prefix: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := receiptMatches(tc.data, tc.stat, tc.receipt, expected, tc.prefix); got != tc.accepted {
				t.Fatalf("receiptMatches = %v, want %v", got, tc.accepted)
			}
		})
	}
}

func TestCheckedResolverEntries(t *testing.T) {
	partial := []string{"owned.conf", "foreign.conf"}
	otherError := errors.New("directory read failed")

	for _, tc := range []struct {
		name      string
		names     []string
		err       error
		wantCount int
		wantError error
	}{
		{name: "complete enumeration", names: partial, wantCount: 2},
		{name: "EOF after entries", names: partial, err: io.EOF, wantCount: 2},
		{name: "wrapped EOF after entries", names: partial, err: fmt.Errorf("read names: %w", io.EOF), wantCount: 2},
		{name: "partial enumeration error", names: partial, err: otherError, wantError: otherError},
		{name: "empty enumeration error", err: otherError, wantError: otherError},
		{name: "too many entries", names: make([]string, 4097), wantError: errUncertain},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := checkedResolverEntries(tc.names, tc.err)
			if tc.wantError != nil {
				if !errors.Is(err, tc.wantError) {
					t.Fatalf("error = %v, want %v", err, tc.wantError)
				}
				if got != nil {
					t.Fatalf("partial names returned on error: %v", got)
				}
				return
			}
			if err != nil || len(got) != tc.wantCount {
				t.Fatalf("entries = %v, error = %v; want %d entries", got, err, tc.wantCount)
			}
		})
	}
}
