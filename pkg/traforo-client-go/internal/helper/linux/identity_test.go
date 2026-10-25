//go:build linux

package helper

import (
	"os"
	"os/exec"
	"testing"

	"golang.org/x/sys/unix"
)

func TestPeerUsesKernelCredentials(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("ordinary-user peer contract")
	}
	pair, e := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if e != nil {
		t.Fatal(e)
	}
	defer func() { _ = unix.Close(pair[0]); _ = unix.Close(pair[1]) }()
	got, e := peer(pair[0])
	want, _, processError := process(int32(os.Getpid()), uint32(os.Getuid()))
	if e != nil || processError != nil || got != want {
		t.Fatalf("peer=%+v want=%+v errors=%v/%v", got, want, e, processError)
	}
	if _, _, e = process(int32(os.Getpid()), uint32(os.Getuid())+1); e == nil {
		t.Fatal("accepted mismatched process UID")
	}
}

func TestPidfdFencesBirthIdentityAndExit(t *testing.T) {
	if os.Getuid() == 0 {
		t.Skip("ordinary-user owner contract")
	}
	cmd := exec.Command("/bin/sleep", "30")
	if e := cmd.Start(); e != nil {
		t.Fatal(e)
	}
	defer func() { _ = cmd.Process.Kill(); _ = cmd.Wait() }()
	owner, _, e := process(int32(cmd.Process.Pid), uint32(os.Getuid()))
	if e != nil {
		t.Fatal(e)
	}
	fd, e := pin(owner)
	if e != nil {
		t.Fatal(e)
	}
	defer func() { _ = unix.Close(fd) }()
	if !alive(owner, fd) {
		t.Fatal("live child rejected")
	}
	for _, changed := range []Owner{
		{owner.UID, owner.PID, owner.Start + 1, owner.Boot},
		{owner.UID, owner.PID, owner.Start, "another-boot"},
	} {
		other, err := pin(changed)
		if err == nil {
			_ = unix.Close(other)
			t.Fatal("accepted changed birth identity")
		}
	}
	if e = cmd.Process.Kill(); e != nil {
		t.Fatal(e)
	}
	_ = cmd.Wait()
	if alive(owner, fd) {
		t.Fatal("dead child retained authority")
	}
}
