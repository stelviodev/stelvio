//go:build darwin

// Read-only harness adapters. The production command never invokes these.
package helper

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"github.com/stelviodev/traforo/darwin"
	"github.com/stelviodev/traforo/protocol"
	"golang.org/x/sys/unix"
	"net"
	"os"
	"time"
)

func FixtureRequest(packet []byte) int {
	r, e := protocol.Decode(packet)
	if e != nil {
		fmt.Fprintln(os.Stderr, "invalid native helper request")
		return 1
	}
	var expected [16]byte
	if r.Operation == protocol.Configure || r.Operation == protocol.Remove || r.Operation == protocol.Release {
		copy(expected[:], "opaque-lease-key")
	}
	ranges := make([]map[string]any, 0, len(r.Ranges))
	for _, p := range r.Ranges {
		n, m := network(p)
		ranges = append(ranges, map[string]any{"cidr": p.String(), "network": n, "mask": m})
	}
	domains := make([]map[string]any, 0, len(r.Resolvers))
	for _, p := range r.Resolvers {
		domains = append(domains, map[string]any{"domain": p.Domain, "port": p.Port})
	}
	json.NewEncoder(os.Stdout).Encode(map[string]any{"operation": r.Operation, "unit": r.Unit, "generation": r.Generation, "keep_dns": r.KeepDNS, "vpc": r.VPC, "session": fmt.Sprintf("%x", r.Session), "capability_matches_fixture": r.Capability == expected, "ranges": ranges, "domains": domains})
	return 0
}
func FixturePeer(path string) int {
	if os.Geteuid() == 0 {
		return 1
	}
	listener, e := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if e != nil {
		return 1
	}
	defer listener.Close()
	listener.SetDeadline(time.Now().Add(5 * time.Second))
	fmt.Println("READY")
	c, e := listener.AcceptUnix()
	if e != nil {
		return 1
	}
	defer c.Close()
	raw, e := c.SyscallConn()
	if e != nil {
		return 1
	}
	var p darwin.Peer
	raw.Control(func(fd uintptr) { p, e = darwin.ReadPeer(int(fd)) })
	if e != nil {
		return 1
	}
	stale := p
	stale.Micros++
	audit := p
	audit.Token[7] ^= 1
	json.NewEncoder(os.Stdout).Encode(map[string]any{"uid": p.UID, "pid": p.PID, "birth_seconds": p.Seconds, "alive": darwin.Alive(p), "stale_rejected": !darwin.Alive(stale), "audit_rejected": !darwin.Alive(audit), "uninstalled_rejected": !trusted()})
	return 0
}
func countFDs() int {
	n := 0
	for fd := 0; fd < 1024; fd++ {
		if _, e := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0); e == nil {
			n++
		}
	}
	return n
}
func FixtureIO(fd int, mode string) int {
	if os.Geteuid() == 0 || fd < 3 || fd >= 1024 {
		return 1
	}
	if e := darwin.Nonblocking(fd); e != nil {
		return 1
	}
	sentinel, e := unix.Open("/dev/null", unix.O_RDONLY|unix.O_CLOEXEC, 0)
	if e != nil {
		return 1
	}
	defer unix.Close(sentinel)
	defer unix.Close(fd)
	before := countFDs()
	fmt.Println("READY")
	valid := false
	c := client{fd: fd, deadline: time.Now().Add(5 * time.Second)}
	var r protocol.Request
	var frame []byte
	for time.Now().Before(c.deadline) {
		fds := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLIN}}
		ready, e := unix.Poll(fds, 100)
		if e == unix.EINTR {
			continue
		}
		if e != nil {
			break
		}
		if ready == 0 {
			continue
		}
		if mode == "--packet" {
			buf := make([]byte, 1517)
			n, e := darwin.Receive(fd, buf)
			if e == unix.EAGAIN || e == unix.EINTR {
				continue
			}
			if e == nil {
				_, frameError := protocol.DecodeFrame(buf[:n])
				if frameError == nil {
					valid = true
					frame = buf[:n]
				}
			}
			break
		}
		var done bool
		r, done, e = c.receive()
		if e != nil {
			break
		}
		if done {
			valid = true
			break
		}
	}
	result := map[string]any{"valid": valid, "before": before, "after": countFDs()}
	_, a := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0)
	_, b := unix.FcntlInt(uintptr(sentinel), unix.F_GETFD, 0)
	result["sentinels_intact"] = a == nil && b == nil
	if mode == "--step" {
		result["raw_size"] = len(c.data)
		result["unit"] = r.Unit
		result["generation"] = r.Generation
	}
	if mode == "--packet" {
		id, generation := uint32(0), uint64(0)
		packet := ""
		if valid {
			id = binary.BigEndian.Uint32(frame)
			generation = binary.BigEndian.Uint64(frame[4:])
			packet = fmt.Sprintf("%x", frame[12:])
		}
		result["unit"] = id
		result["generation"] = generation
		result["packet"] = packet
	}
	json.NewEncoder(os.Stdout).Encode(result)
	return 0
}
