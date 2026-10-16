//go:build darwin

package darwin

import (
	"fmt"
	"golang.org/x/sys/unix"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"testing"
	"time"
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

func TestOwnerFixture(t *testing.T) {
	mode := os.Getenv("TRAFORO_OWNER_FIXTURE")
	if mode == "" {
		return
	}
	if mode == "leaf" {
		c, e := net.Dial("unix", os.Getenv("TRAFORO_OWNER_SOCKET"))
		if e != nil {
			os.Exit(3)
		}
		defer c.Close()
		c.Write([]byte("ready"))
		var b [1]byte
		c.Read(b[:])
		return
	}
	cmd := exec.Command(os.Args[0], "-test.run=^TestOwnerFixture$")
	cmd.Env = append(os.Environ(), "TRAFORO_OWNER_FIXTURE=leaf")
	if e := cmd.Start(); e != nil {
		os.Exit(4)
	}
	cmd.Wait()
}

func TestParentDeathWithLiveRuntime(t *testing.T) {
	directory, e := os.MkdirTemp("/private/tmp", "tf-owner-")
	if e != nil {
		t.Fatal(e)
	}
	defer os.RemoveAll(directory)
	path := filepath.Join(directory, "s")
	listener, e := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if e != nil {
		t.Fatal(e)
	}
	defer listener.Close()
	listener.SetDeadline(time.Now().Add(10 * time.Second))
	cmd := exec.Command(os.Args[0], "-test.run=^TestOwnerFixture$")
	cmd.Env = append(os.Environ(), "TRAFORO_OWNER_FIXTURE=parent", "TRAFORO_OWNER_SOCKET="+path)
	if e = cmd.Start(); e != nil {
		t.Fatal(e)
	}
	defer func() { cmd.Process.Kill(); cmd.Wait() }()
	c, e := listener.AcceptUnix()
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	raw, _ := c.SyscallConn()
	var peer Peer
	raw.Control(func(fd uintptr) { peer, e = ReadPeer(int(fd)) })
	if e != nil {
		t.Fatal(e)
	}
	parent, e := Parent(peer)
	if e != nil || parent.PID != int32(cmd.Process.Pid) || !ParentAlive(parent) {
		t.Fatal(parent, e)
	}
	stale := parent
	stale.Micros++
	if ParentAlive(stale) {
		t.Fatal("accepted stale parent birth")
	}
	cmd.Process.Kill()
	cmd.Wait()
	if !Alive(peer) {
		t.Fatal("runtime should remain live for this proof")
	}
	if ParentAlive(parent) {
		t.Fatal("dead command retained host authority")
	}
	c.Write([]byte("q"))
}

func TestExtendedACLRejected(t *testing.T) {
	for _, directory := range []bool{false, true} {
		t.Run(fmt.Sprint(directory), func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "owned")
			if directory {
				if e := os.Mkdir(path, 0700); e != nil {
					t.Fatal(e)
				}
			} else {
				if e := os.WriteFile(path, nil, 0600); e != nil {
					t.Fatal(e)
				}
			}
			if !NoACLPath(path) {
				t.Fatal("plain file has ACL")
			}
			acl := "everyone allow read,write"
			if directory {
				acl += ",file_inherit,directory_inherit"
			}
			if out, e := exec.Command("/bin/chmod", "+a", acl, path).CombinedOutput(); e != nil {
				t.Fatalf("chmod: %v %s", e, out)
			}
			if NoACLPath(path) {
				t.Fatal("extended ACL accepted")
			}
			if directory {
				child := filepath.Join(path, "child")
				if e := os.WriteFile(child, nil, 0600); e != nil {
					t.Fatal(e)
				}
				if NoACLPath(child) {
					t.Fatal("inherited ACL accepted")
				}
			}
		})
	}
}

func TestAncillaryAbuseAcrossStreamAndDatagram(t *testing.T) {
	for _, kind := range []int{unix.SOCK_STREAM, unix.SOCK_DGRAM} {
		for _, count := range []int{1, 33, 128, 254, 255} {
			for _, empty := range []bool{false, true} {
				if empty && kind == unix.SOCK_STREAM {
					continue
				}
				t.Run(fmt.Sprintf("kind%d/rights%d/empty%v", kind, count, empty), func(t *testing.T) {
					pair, e := unix.Socketpair(unix.AF_UNIX, kind, 0)
					if e != nil {
						t.Fatal(e)
					}
					defer unix.Close(pair[0])
					defer unix.Close(pair[1])
					source, e := unix.Open("/dev/null", unix.O_RDONLY, 0)
					if e != nil {
						t.Fatal(e)
					}
					defer unix.Close(source)
					rights := make([]int, count)
					for i := range rights {
						rights[i] = source
					}
					inventory := func() map[int]int {
						fds := map[int]int{}
						for fd := 0; fd < 1024; fd++ {
							if f, e := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0); e == nil {
								fds[fd] = f
							}
						}
						return fds
					}
					before := inventory()
					payload := []byte("x")
					if empty {
						payload = nil
					}
					e = unix.Sendmsg(pair[0], payload, unix.UnixRights(rights...), nil, 0)
					if e == unix.EINVAL || e == unix.EMSGSIZE {
						if count < 255 {
							t.Fatal(e)
						}
					} else {
						if e != nil {
							t.Fatal(e)
						}
						if _, e = Receive(pair[1], make([]byte, 128)); e == nil {
							t.Fatal("accepted ancillary rights")
						}
					}
					if !reflect.DeepEqual(inventory(), before) {
						t.Fatal("descriptor inventory changed")
					}
					if _, e := unix.FcntlInt(uintptr(source), unix.F_GETFD, 0); e != nil {
						t.Fatal("closed existing sentinel")
					}
				})
			}
		}
	}
}
