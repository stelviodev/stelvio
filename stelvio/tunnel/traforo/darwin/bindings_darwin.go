//go:build darwin

// Package darwin adapts native facts and operations; callers own authorization.
package darwin

/*
#cgo LDFLAGS: -lbsm -framework SystemConfiguration -framework CoreFoundation
#include "bindings.h"
#include <stdlib.h>
*/
import "C"
import (
	"bytes"
	"encoding/binary"
	"errors"
	"golang.org/x/sys/unix"
	"net"
	"os"
	"runtime"
	"stelvio.dev/traforo/protocol"
	"strconv"
	"strings"
	"unsafe"
)

var Err = errors.New("Darwin operation unavailable or uncertain")

const Installed = "/Library/PrivilegedHelperTools/stelvio-traforo"
const StateParent = "/Library/Application Support/Stelvio"
const State = StateParent + "/tunnel"
const Socket = State + "/helper.sock"
const Plist = "/Library/LaunchDaemons/dev.stelvio.tunnel.plist"

// Peer aliases the durable v3 representation; live authority comes from the
// audit-token backend, never from deserialized fields alone.
type Peer = protocol.DarwinOwner

func validPeer(p Peer) bool { return p.ValidV3() }
func nativePeer(p Peer) C.tf_peer {
	var v C.tf_peer
	v.uid = C.uint32_t(p.UID)
	v.pid = C.int32_t(p.PID)
	v.seconds = C.uint64_t(p.Seconds)
	v.micros = C.uint64_t(p.Micros)
	for i, x := range p.Token {
		v.token[i] = C.uint32_t(x)
	}
	return v
}
func ReadPeer(fd int) (Peer, error) {
	var v C.tf_peer
	if C.tf_peer_read(C.int(fd), &v) != 0 {
		return Peer{}, Err
	}
	p := Peer{UID: uint32(v.uid), PID: int32(v.pid), Seconds: uint64(v.seconds), Micros: uint64(v.micros)}
	for i := range p.Token {
		p.Token[i] = uint32(v.token[i])
	}
	if !validPeer(p) {
		return Peer{}, Err
	}
	return p, nil
}
func Alive(p Peer) bool { v := nativePeer(p); return validPeer(p) && C.tf_peer_live(&v) != 0 }
func NoACL(fd int) bool { return C.tf_no_acl(C.int(fd)) != 0 }
func NoACLPath(path string) bool {
	var before, after unix.Stat_t
	if unix.Lstat(path, &before) != nil {
		return false
	}
	value := C.CString(path)
	defer C.free(unsafe.Pointer(value))
	return C.tf_no_acl_path(value) != 0 && unix.Lstat(path, &after) == nil && before.Dev == after.Dev && before.Ino == after.Ino
}
func Image() (string, uint64, uint64, error) {
	var dev, ino C.uint64_t
	buf := make([]byte, 4096)
	if C.tf_image(&dev, &ino, (*C.char)(unsafe.Pointer(&buf[0])), C.size_t(len(buf))) != 0 {
		return "", 0, 0, Err
	}
	end := bytes.IndexByte(buf, 0)
	if end < 0 {
		return "", 0, 0, Err
	}
	return string(buf[:end]), uint64(dev), uint64(ino), nil
}
func Supported() error {
	if runtime.GOARCH != "arm64" && runtime.GOARCH != "amd64" {
		return Err
	}
	version, e := unix.Sysctl("kern.osproductversion")
	if e != nil {
		return Err
	}
	major, e := strconv.Atoi(strings.Split(version, ".")[0])
	if e != nil || major < 15 {
		return Err
	}
	return nil
}
func Nonblocking(fd int) error {
	if e := unix.SetNonblock(fd, true); e != nil {
		return e
	}
	unix.CloseOnExec(fd)
	return unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_NOSIGPIPE, 1)
}
func Listener() (int, error) {
	fd := int(C.tf_listener())
	if fd < 0 {
		return -1, Err
	}
	fail := func() (int, error) { unix.Close(fd); return -1, Err }
	addr, e := unix.Getsockname(fd)
	a, ok := addr.(*unix.SockaddrUnix)
	if e != nil || !ok || a.Name != Socket {
		return fail()
	}
	var st unix.Stat_t
	if unix.Lstat(Socket, &st) != nil || st.Uid != 0 || st.Mode&unix.S_IFMT != unix.S_IFSOCK || st.Mode&0777 != 0666 {
		return fail()
	}
	acl := NoACLPath(Socket)
	typ, e := unix.GetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_TYPE)
	if !acl || e != nil || typ != unix.SOCK_STREAM || Nonblocking(fd) != nil || unix.Listen(fd, unix.SOMAXCONN) != nil {
		return fail()
	}
	return fd, nil
}

type Device struct {
	FD    int
	Name  string
	Index uint32
}

func OpenDevice() (Device, error) {
	buf := make([]byte, 16)
	fd := int(C.tf_utun((*C.char)(unsafe.Pointer(&buf[0])), C.size_t(len(buf))))
	if fd < 0 {
		return Device{FD: -1}, Err
	}
	name := strings.TrimRight(string(buf), "\x00")
	iface, e := net.InterfaceByName(name)
	if e != nil || !strings.HasPrefix(name, "utun") || Nonblocking(fd) != nil {
		unix.Close(fd)
		return Device{FD: -1}, Err
	}
	return Device{fd, name, uint32(iface.Index)}, nil
}
func (d Device) Current() bool {
	if d.FD < 0 {
		return false
	}
	s := C.CString(d.Name)
	defer C.free(unsafe.Pointer(s))
	return C.tf_if_current(C.int(d.FD), s, C.uint32_t(d.Index)) != 0
}
func (d *Device) Close() error {
	fd := d.FD
	*d = Device{FD: -1}
	if fd >= 0 {
		return unix.Close(fd)
	}
	return nil
}
func (d Device) Configure(local uint32) error {
	s := C.CString(d.Name)
	defer C.free(unsafe.Pointer(s))
	if C.tf_configure(s, C.uint32_t(local), C.uint32_t(local+1)) != 0 {
		return Err
	}
	return nil
}
func (d Device) AddRoute(network, mask uint32) error {
	if C.tf_route_add(C.uint32_t(d.Index), C.uint32_t(network), C.uint32_t(mask)) != 0 {
		return Err
	}
	return nil
}

type Route struct{ Network, Mask, Index uint32 }

func Routes() ([]Route, error) {
	native := make([]C.tf_route, 65536)
	var count C.size_t
	if C.tf_routes(&native[0], C.size_t(len(native)), &count) != 0 {
		return nil, Err
	}
	out := make([]Route, int(count))
	for i := range out {
		out[i] = Route{uint32(native[i].network), uint32(native[i].mask), uint32(native[i].index)}
	}
	return out, nil
}
func DNSDomains() ([]string, error) {
	buf := make([]byte, 4*1024*1024)
	var n C.size_t
	if C.tf_dns((*C.char)(unsafe.Pointer(&buf[0])), C.size_t(len(buf)), &n) != 0 {
		return nil, Err
	}
	if n == 0 {
		return nil, nil
	}
	return strings.Split(strings.TrimSuffix(string(buf[:int(n)]), "\x00"), "\x00"), nil
}
func RenameExclusive(from int, source string, to int, target string) error {
	a, b := C.CString(source), C.CString(target)
	defer C.free(unsafe.Pointer(a))
	defer C.free(unsafe.Pointer(b))
	v, e := C.tf_rename_excl(C.int(from), a, C.int(to), b)
	if v != 0 {
		if e != nil {
			return e
		}
		return Err
	}
	return nil
}

// Incoming descriptors are never accepted. Receive all copied ancillary data,
// close every delivered FD even on truncation, then reject any control payload.
const ControlBytes = 65536

func Receive(fd int, buf []byte) (int, error) {
	control := make([]byte, ControlBytes)
	n, oob, flags, _, e := unix.Recvmsg(fd, buf, control, unix.MSG_DONTWAIT)
	if e != nil {
		return n, e
	}
	bad := oob != 0 || flags&(unix.MSG_CTRUNC|unix.MSG_TRUNC) != 0
	// Parse each available header independently: ParseSocketControlMessage can
	// return no messages for a malformed trailing header, leaking earlier rights.
	for pos := 0; pos+unix.CmsgLen(0) <= oob; {
		header := (*unix.Cmsghdr)(unsafe.Pointer(&control[pos]))
		length := int(header.Len)
		if length < unix.CmsgLen(0) {
			break
		}
		available := length
		if available > oob-pos {
			available = oob - pos
		}
		if header.Level == unix.SOL_SOCKET && header.Type == unix.SCM_RIGHTS {
			for i := pos + unix.CmsgLen(0); i+4 <= pos+available; i += 4 {
				fd := int(int32(binary.NativeEndian.Uint32(control[i:])))
				unix.Close(fd)
			}
		}
		padded := unix.CmsgSpace(length - unix.CmsgLen(0))
		if padded <= 0 || padded > oob-pos {
			break
		}
		pos += padded
	}
	if bad {
		return n, Err
	}
	return n, nil
}
func Pair() ([2]int, error) {
	p, e := unix.Socketpair(unix.AF_UNIX, unix.SOCK_DGRAM, 0)
	if e != nil {
		return p, e
	}
	for _, fd := range p {
		if Nonblocking(fd) != nil {
			unix.Close(p[0])
			unix.Close(p[1])
			return p, Err
		}
		if unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_SNDBUF, 256*1024) != nil || unix.SetsockoptInt(fd, unix.SOL_SOCKET, unix.SO_RCVBUF, 256*1024) != nil {
			unix.Close(p[0])
			unix.Close(p[1])
			return p, Err
		}
	}
	return p, nil
}
func Root() bool { return os.Geteuid() == 0 && os.Getuid() == 0 }
