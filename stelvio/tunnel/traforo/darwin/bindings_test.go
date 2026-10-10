//go:build darwin

package darwin

import (
	"golang.org/x/sys/unix"
	"net"
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

func TestRejectedDescriptorsAreClosed(t *testing.T) {
	pair, e := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM, 0)
	if e != nil {
		t.Fatal(e)
	}
	defer unix.Close(pair[0])
	defer unix.Close(pair[1])
	file, e := os.CreateTemp(t.TempDir(), "rights")
	if e != nil {
		t.Fatal(e)
	}
	defer file.Close()
	inventory := func() map[int]int {
		fds := make(map[int]int)
		for fd := 0; fd < 1024; fd++ {
			if flags, err := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0); err == nil {
				fds[fd] = flags
			}
		}
		return fds
	}
	before := inventory()
	for i := 0; i < 100; i++ {
		if e = unix.Sendmsg(pair[0], []byte("x"), unix.UnixRights(int(file.Fd())), nil, 0); e != nil {
			t.Fatal(e)
		}
		if _, e = Receive(pair[1], make([]byte, 1)); e == nil {
			t.Fatal("accepted descriptor")
		}
	}
	if after := inventory(); !reflect.DeepEqual(before, after) {
		t.Fatalf("rejected rights changed descriptor inventory: before=%v after=%v", before, after)
	}
}
func TestNativeImageAndCapabilityQueries(t *testing.T) {
	path, dev, ino, e := Image()
	if e != nil || path == "" || dev == 0 || ino == 0 {
		t.Fatal(path, dev, ino, e)
	}
	var st unix.Stat_t
	if statError := unix.Lstat(path, &st); statError != nil || uint64(uint32(st.Dev)) != dev || st.Ino != ino {
		t.Fatalf("loaded image mismatch: path=%q error=%v native=%d:%d stat=%d:%d", path, statError, dev, ino, st.Dev, st.Ino)
	}
	if e = Supported(); e != nil {
		t.Fatal(e)
	}
	if _, e = Routes(); e != nil {
		t.Fatal(e)
	}
	if _, e = DNSDomains(); e != nil {
		t.Fatal(e)
	}
	if NoACL(-1) {
		t.Fatal("invalid FD")
	}
}

func TestUnixSocketACLInventory(t *testing.T) {
	path := filepath.Join(t.TempDir(), "control.sock")
	listener, e := net.Listen("unix", path)
	if e != nil {
		t.Fatal(e)
	}
	defer listener.Close()
	if !NoACLPath(path) {
		t.Fatal("plain Unix socket ACL query failed")
	}
}
