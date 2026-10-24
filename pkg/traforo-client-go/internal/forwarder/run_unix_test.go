//go:build darwin || linux

package forwarder

import (
	"os"
	"strings"
	"syscall"
	"testing"
	"time"
)

func TestUnprivilegedIdentityGuard(t *testing.T) {
	for _, tc := range []struct {
		name            string
		real, effective int
		want            bool
	}{
		{name: "ordinary user", real: 501, effective: 501, want: true},
		{name: "root", real: 0, effective: 0},
		{name: "setuid root", real: 501, effective: 0},
		{name: "changed identity", real: 0, effective: 501},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := unprivileged(tc.real, tc.effective); got != tc.want {
				t.Fatalf("unprivileged(%d, %d) = %v, want %v", tc.real, tc.effective, got, tc.want)
			}
		})
	}
}

func TestCarrierFromFileRequiresConnectedDatagramSocket(t *testing.T) {
	fds, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_STREAM, 0)
	if err != nil {
		t.Fatal(err)
	}
	file := os.NewFile(uintptr(fds[0]), "stream-carrier")
	peer := os.NewFile(uintptr(fds[1]), "stream-peer")
	defer func() { _ = peer.Close() }()
	if _, err := carrierFromFile(file); err == nil || !strings.Contains(err.Error(), "Unix datagram") {
		t.Fatalf("stream socket error = %v", err)
	}
}

func TestCarrierFromFileDuplicatesDatagramSocket(t *testing.T) {
	fds, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_DGRAM, 0)
	if err != nil {
		t.Fatal(err)
	}
	file := os.NewFile(uintptr(fds[0]), "datagram-carrier")
	peer := os.NewFile(uintptr(fds[1]), "datagram-peer")
	defer func() { _ = peer.Close() }()
	carrier, err := carrierFromFile(file)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = carrier.Close() }()
	if _, err := peer.Write([]byte("carrier")); err != nil {
		t.Fatal(err)
	}
	_ = carrier.SetReadDeadline(time.Now().Add(time.Second))
	buffer := make([]byte, 32)
	n, err := carrier.Read(buffer)
	if err != nil || string(buffer[:n]) != "carrier" {
		t.Fatalf("read carrier datagram %q: %v", buffer[:n], err)
	}
}
