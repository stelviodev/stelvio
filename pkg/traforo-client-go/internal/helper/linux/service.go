//go:build linux

package helper

import (
	"bytes"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"math"
	"net"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"

	"github.com/stelviodev/traforo/internal/diagnostics"
	"github.com/stelviodev/traforo/internal/protocol"
	"golang.org/x/sys/unix"
)

const (
	ok uint16 = iota
	busy
	invalid
	uncertain
	unauthorized
)

type service struct {
	resolverEpoch              string
	admissionGate              int
	mu                         sync.Mutex
	state                      Snapshot
	owned, uncertain, stopping bool
	lease                      *net.UnixConn
	carrier                    int
	ownerFD, parentFD          int
	devices                    [8]int
	metrics                    map[uint32]*packetMetrics
	epoch                      string
	started                    time.Time
	lastDNS                    time.Time
	lastFailure                string
}

func newService() *service {
	s := &service{carrier: -1, ownerFD: -1, parentFD: -1, admissionGate: -1, metrics: map[uint32]*packetMetrics{}, started: time.Now()}
	for i := range s.devices {
		s.devices[i] = -1
	}
	var epoch [16]byte
	_, _ = rand.Read(epoch[:])
	s.epoch = hex.EncodeToString(epoch[:])
	return s
}
func reply(c *net.UnixConn, status uint16, body []byte, fd int) error {
	packet := make([]byte, 16+len(body))
	copy(packet, "STLVREP1")
	binary.BigEndian.PutUint16(packet[8:], status)
	binary.BigEndian.PutUint32(packet[12:], uint32(len(body)))
	copy(packet[16:], body)
	_ = c.SetWriteDeadline(time.Now().Add(time.Second))
	var control []byte
	if fd >= 0 {
		control = unix.UnixRights(fd)
	}
	n, _, e := c.WriteMsgUnix(packet, control, nil)
	if e != nil {
		return e
	}
	for n < len(packet) {
		written, e := c.Write(packet[n:])
		if e != nil {
			return e
		}
		if written == 0 {
			return io.ErrShortWrite
		}
		n += written
	}
	return nil
}
func readRequest(c *net.UnixConn) (protocol.Request, error) {
	packet := make([]byte, protocol.Header)
	// recvmsg refuses unexpected descriptor transfers and closes all received FDs.
	read := func(p []byte) error {
		for len(p) > 0 {
			oob := make([]byte, 4096)
			n, count, flags, _, e := c.ReadMsgUnix(p, oob)
			if count > 0 {
				messages, _ := unix.ParseSocketControlMessage(oob[:count])
				for _, m := range messages {
					fds, _ := unix.ParseUnixRights(&m)
					for _, fd := range fds {
						_ = unix.Close(fd)
					}
				}
			}
			if e != nil {
				return e
			}
			if count != 0 || flags&(unix.MSG_TRUNC|unix.MSG_CTRUNC) != 0 {
				return protocol.ErrInvalid
			}
			if n == 0 {
				return io.ErrUnexpectedEOF
			}
			p = p[n:]
		}
		return nil
	}
	if e := read(packet); e != nil {
		return protocol.Request{}, e
	}
	size := binary.BigEndian.Uint32(packet[12:])
	if size > protocol.MaxBody {
		return protocol.Request{}, protocol.ErrInvalid
	}
	packet = append(packet, make([]byte, int(size))...)
	if e := read(packet[protocol.Header:]); e != nil {
		return protocol.Request{}, e
	}
	return protocol.Decode(packet)
}
func connectionPeer(c *net.UnixConn) (Owner, error) {
	raw, e := c.SyscallConn()
	if e != nil {
		return Owner{}, e
	}
	var p Owner
	var result error
	e = raw.Control(func(fd uintptr) { p, result = peer(int(fd)) })
	return p, errors.Join(e, result)
}
func (s *service) handle(c *net.UnixConn) bool {
	_ = c.SetDeadline(time.Now().Add(5 * time.Second))
	p, e := connectionPeer(c)
	if e != nil {
		_ = reply(c, unauthorized, nil, -1)
		return false
	}
	r, e := readRequest(c)
	if e != nil {
		_ = reply(c, invalid, nil, -1)
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if r.Operation == protocol.Status {
		_ = reply(c, ok, s.status(), -1)
		return false
	}
	if r.Operation == protocol.Diagnostics {
		body, e := json.Marshal(s.diagnostics(p.UID))
		if e == nil && len(body) <= diagnostics.Limit {
			_ = reply(c, ok, body, -1)
		}
		return false
	}
	if s.stopping {
		_ = reply(c, busy, nil, -1)
		return false
	}
	if r.Operation == protocol.Reconcile {
		if s.lease != nil || s.owned && s.state.Peer.UID != p.UID {
			_ = reply(c, busy, nil, -1)
		} else if e = s.cleanup(); e != nil {
			_ = reply(c, uncertain, nil, -1)
		} else {
			_ = reply(c, ok, nil, -1)
		}
		return false
	}
	if r.Operation == protocol.Acquire {
		gate, gateError := sharedAdmission()
		if gateError != nil {
			_ = reply(c, busy, nil, -1)
			return false
		}
		defer func() {
			if gate >= 0 {
				_ = unix.Close(gate)
			}
		}()

		if s.owned || s.uncertain {
			_ = reply(c, busy, nil, -1)
			return false
		}
		ownerFD, e := pin(p)
		if e != nil {
			_ = reply(c, unauthorized, nil, -1)
			return false
		}
		_, ppid, e := process(p.PID, p.UID)
		parent, _, parentErr := process(ppid, p.UID)
		if e != nil || parentErr != nil {
			_ = unix.Close(ownerFD)
			_ = reply(c, unauthorized, nil, -1)
			return false
		}
		parentFD, e := pin(parent)
		if e != nil {
			_ = unix.Close(ownerFD)
			_ = reply(c, unauthorized, nil, -1)
			return false
		}
		pair, e := unix.Socketpair(unix.AF_UNIX, unix.SOCK_DGRAM|unix.SOCK_CLOEXEC|unix.SOCK_NONBLOCK, 0)
		if e != nil {
			_ = unix.Close(ownerFD)
			_ = unix.Close(parentFD)
			_ = reply(c, uncertain, nil, -1)
			return false
		}
		s.state = Snapshot{Version: 1, Peer: p, Parent: parent, Session: r.Session, Units: []Unit{}}
		if _, e = rand.Read(s.state.Capability[:]); e == nil {
			e = save(&s.state)
		}
		if e != nil {
			_ = unix.Close(pair[0])
			_ = unix.Close(pair[1])
			_ = unix.Close(ownerFD)
			_ = unix.Close(parentFD)
			s.uncertain = true
			_ = reply(c, uncertain, nil, -1)
			return false
		}
		s.owned = true
		s.metrics = map[uint32]*packetMetrics{}
		s.lastFailure = ""
		s.admissionGate = gate
		gate = -1
		s.carrier = pair[0]
		s.lease = c
		s.ownerFD = ownerFD
		s.parentFD = parentFD
		body := make([]byte, 20)
		copy(body, s.state.Capability[:])
		binary.BigEndian.PutUint32(body[16:], 1)
		e = reply(c, ok, body, pair[1])
		_ = unix.Close(pair[1])
		if e != nil {
			s.revoke()
			return false
		}
		_ = c.SetDeadline(time.Time{})
		go func() {
			var b [1]byte
			_, _ = c.Read(b[:])
			s.mu.Lock()
			defer s.mu.Unlock()
			if s.lease == c {
				s.lastFailure = "lease-eof"
				s.revoke()
			}
		}()
		return true
	}
	if !s.owned || s.lease == nil || p != s.state.Peer || r.Session != s.state.Session || r.Capability != s.state.Capability || !alive(s.state.Peer, s.ownerFD) || !alive(s.state.Parent, s.parentFD) {
		_ = reply(c, unauthorized, nil, -1)
		return false
	}
	if s.uncertain {
		_ = reply(c, uncertain, nil, -1)
		return false
	}
	switch r.Operation {
	case protocol.Configure:
		e = s.configure(r)
	case protocol.Remove:
		e = s.remove(r)
	case protocol.Release:
		s.revoke()
		e = s.cleanup()
	default:
		_ = reply(c, invalid, nil, -1)
		return false
	}
	if e != nil {
		s.lastFailure = e.Error()
		_ = reply(c, uncertain, nil, -1)
	} else {
		_ = reply(c, ok, nil, -1)
	}
	return false
}
func (s *service) status() []byte {
	count := len(s.state.Units)
	body := make([]byte, 21+13*count)
	copy(body, "STLVHLP1")
	binary.BigEndian.PutUint32(body[8:], 1)
	binary.BigEndian.PutUint32(body[12:], 1)
	var flags uint32
	if s.owned {
		flags |= 1
	}
	if s.uncertain {
		flags |= 2
	}
	binary.BigEndian.PutUint32(body[16:], flags)
	body[20] = byte(count)
	for i, u := range s.state.Units {
		p := body[21+13*i:]
		binary.BigEndian.PutUint32(p, u.Request.Unit)
		binary.BigEndian.PutUint64(p[4:], u.Request.Generation)
		status := byte(3)
		if u.Phase == 2 {
			status = 1
		}
		if u.Phase == 4 {
			status = 4
		}
		if u.Phase == 5 {
			status = 0
		}
		p[12] = status
	}
	return body
}
func (s *service) configure(r protocol.Request) error {
	if r.Generation == math.MaxUint64 {
		return protocol.ErrInvalid
	}
	index := -1
	added := false
	slots := map[int]bool{}
	for i, u := range s.state.Units {
		slots[u.Slot] = true
		if u.Request.Unit == r.Unit {
			index = i
			continue
		}
		if u.Phase == 5 {
			continue
		}
		for _, p := range r.Ranges {
			for _, q := range u.Request.Ranges {
				if p.Overlaps(q) {
					return protocol.ErrInvalid
				}
			}
		}
		for _, p := range r.Resolvers {
			for _, q := range u.Request.Resolvers {
				if protocol.DNSOverlap(p.Domain, q.Domain) {
					return protocol.ErrInvalid
				}
			}
		}
	}
	if index >= 0 {
		u := s.state.Units[index]
		if u.Phase == 2 && r.Generation == u.Request.Generation {
			a, _ := json.Marshal(u.Request)
			b, _ := json.Marshal(r)
			if bytes.Equal(a, b) {
				return s.verify(u)
			}
			if !protocol.Extension(u.Request, r) {
				return protocol.ErrInvalid
			}
			u.Request = r
			if e := resolverConflict(u); e != nil {
				return e
			}
			s.state.Units[index] = u
			if e := save(&s.state); e != nil {
				return e
			}
			return configureDNS(u)
		}
		if r.Generation <= u.Request.Generation || (u.Phase != 4 && u.Phase != 5) {
			return protocol.ErrInvalid
		}
		if u.Phase == 4 {
			if r.VPC != u.Request.VPC || !equalRanges(u.Request, r) {
				return protocol.ErrInvalid
			}
			for _, old := range u.Request.Resolvers {
				found := false
				for _, v := range r.Resolvers {
					if v == old {
						found = true
					}
				}
				if !found {
					return protocol.ErrInvalid
				}
			}
		}
	} else {
		if len(s.state.Units) >= 8 {
			return protocol.ErrInvalid
		}
		slot := 0
		for slots[slot] {
			slot++
		}
		tun, dns, alias := names(s.state, r.Unit)
		s.state.Units = append(s.state.Units, Unit{Request: r, Slot: slot, Phase: 1, TUN: tun, DNS: dns, Alias: alias, Local: uint32(0xc0000201 + 2*slot)})
		index = len(s.state.Units) - 1
		added = true
	}
	u := s.state.Units[index]
	retained := u.Phase == 4
	failPreflight := func(e error) error {
		if added {
			s.state.Units = s.state.Units[:index]
		}
		return e
	}
	if u.Phase == 5 {
		u.DNSIndex = 0
	}
	u.Request = r
	u.Phase = 1
	u.TUNIndex = 0
	if e := resourceConflict(u, retained); e != nil {
		return failPreflight(e)
	}
	if e := routeConflict(r, 0); e != nil {
		return failPreflight(e)
	}
	if e := resolverConflict(u); e != nil {
		return failPreflight(e)
	}
	s.state.Units[index] = u
	if e := save(&s.state); e != nil {
		s.uncertain = true
		return e
	}
	if e := createDNS(&u); e != nil {
		return s.configureFailed(index, u, e)
	}
	s.state.Units[index] = u
	if e := save(&s.state); e != nil {
		return s.configureFailed(index, u, e)
	}
	fd, e := createTUN(&u)
	if e != nil {
		return s.configureFailed(index, u, e)
	}
	s.devices[u.Slot] = fd
	s.state.Units[index] = u
	if e = save(&s.state); e == nil {
		e = configureDNS(u)
	}
	if e == nil {
		e = s.verify(u)
	}
	if e != nil {
		return s.configureFailed(index, u, e)
	}
	s.resolverEpoch, _ = resolvedOwner()
	u.Phase = 2
	s.state.Units[index] = u
	if e = save(&s.state); e != nil {
		return s.configureFailed(index, u, e)
	}
	return nil
}
func equalRanges(a, b protocol.Request) bool {
	if len(a.Ranges) != len(b.Ranges) {
		return false
	}
	for i, p := range a.Ranges {
		if p != b.Ranges[i] {
			return false
		}
	}
	return true
}
func (s *service) configureFailed(index int, u Unit, cause error) error {
	s.state.Units[index] = u
	s.state.Units[index].Phase = 3
	if e := save(&s.state); e != nil {
		s.uncertain = true
		return errors.Join(cause, e)
	}
	if e := s.dispose(index, false); e != nil {
		s.uncertain = true
		return errors.Join(cause, e)
	}
	return cause
}
func (s *service) verify(u Unit) error {
	if _, e := ownedLink(u.TUN, u.Alias, "tuntap", u.TUNIndex); e != nil {
		return e
	}
	if e := routeConflict(u.Request, u.TUNIndex); e != nil {
		return e
	}
	if e := verifyRoutes(u); e != nil {
		return e
	}
	if e := resolverConflict(u); e != nil {
		return e
	}
	return verifyDNS(u)
}
func (s *service) remove(r protocol.Request) error {
	for i, u := range s.state.Units {
		if u.Request.Unit != r.Unit {
			continue
		}
		if u.Phase == 5 {
			if !r.KeepDNS && r.Generation == u.Request.Generation {
				return nil
			}
			return protocol.ErrInvalid
		}
		if u.Phase == 4 && r.KeepDNS && r.Generation == u.Request.Generation {
			return nil
		}
		if r.Generation <= u.Request.Generation || r.Generation == math.MaxUint64 {
			return protocol.ErrInvalid
		}
		s.state.Units[i].Request.Generation = r.Generation
		s.state.Units[i].Phase = 3
		if e := save(&s.state); e != nil {
			s.uncertain = true
			return e
		}
		return s.dispose(i, r.KeepDNS)
	}
	return protocol.ErrInvalid
}
func (s *service) dispose(i int, keepDNS bool) error {
	u := s.state.Units[i]
	// Verify live link identity before closing its FD or deleting recorded resources.
	if l, e := ownedLink(u.TUN, u.Alias, "tuntap", u.TUNIndex); e == nil {
		_ = l
		if s.devices[u.Slot] >= 0 {
			_ = unix.Close(s.devices[u.Slot])
			s.devices[u.Slot] = -1
		} else if e = deleteLink(u.TUN, u.Alias, "tuntap", u.TUNIndex); e != nil {
			return e
		}
	} else if !absent(e) {
		return e
	} else if s.devices[u.Slot] >= 0 {
		_ = unix.Close(s.devices[u.Slot])
		s.devices[u.Slot] = -1
	}
	if keepDNS {
		if e := verifyDNS(u); e != nil {
			return e
		}
		u.Phase = 4
	} else {
		if e := deleteLink(u.DNS, u.Alias, "dummy", u.DNSIndex); e != nil {
			return e
		}
		u.Phase = 5
	}
	s.state.Units[i] = u
	if e := save(&s.state); e != nil {
		s.uncertain = true
		return e
	}
	return nil
}
func (s *service) revoke() {
	if s.lease != nil {
		_ = s.lease.Close()
		s.lease = nil
	}
	for _, fd := range []int{s.carrier, s.ownerFD, s.parentFD, s.admissionGate} {
		if fd >= 0 {
			_ = unix.Close(fd)
		}
	}
	s.carrier = -1
	s.ownerFD = -1
	s.parentFD = -1
	s.admissionGate = -1
}
func (s *service) cleanup() error {
	if !s.owned {
		if s.uncertain {
			return errUncertain
		}
		return nil
	}
	s.revoke()
	for i := range s.state.Units {
		if e := s.dispose(i, false); e != nil {
			s.uncertain = true
			s.lastFailure = e.Error()
			return e
		}
	}
	if e := clearJournal(); e != nil {
		s.uncertain = true
		return e
	}
	s.state = Snapshot{}
	s.owned = false
	s.uncertain = false
	return nil
}
func (s *service) poll() {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.lease != nil && (!alive(s.state.Peer, s.ownerFD) || !alive(s.state.Parent, s.parentFD)) {
		s.lastFailure = "owner-or-parent-exited"
		s.revoke()
	}
	if s.owned && s.lease == nil {
		_ = s.cleanup()
		return
	}
	if s.carrier < 0 {
		return
	}
	s.pumpReturn()
	for _, u := range s.state.Units {
		if u.Phase == 2 {
			s.pumpHost(u)
		}
	}
	if time.Since(s.lastDNS) > time.Second {
		s.lastDNS = time.Now()
		epoch, epochError := resolvedOwner()
		restarted := epochError == nil && s.resolverEpoch != "" && epoch != s.resolverEpoch
		if epochError == nil {
			s.resolverEpoch = epoch
		}
		for i, u := range s.state.Units {
			if u.Phase != 2 && u.Phase != 4 {
				continue
			}
			// Recheck foreign claims as well as our own link. resolved can route
			// matching queries to several links when another administrator adds
			// an overlapping domain after activation.
			e := resolverConflict(u)
			if e == nil {
				e = verifyDNS(u)
			}
			if e != nil || restarted {
				s.lastFailure = "resolver-configuration-lost"
				s.state.Units[i].Phase = 3
				if e = save(&s.state); e != nil {
					s.uncertain = true
					s.revoke()
					return
				}
				// Stop forwarding before repairing DNS; readiness must be regained with a newer generation.
				if fd := s.devices[u.Slot]; fd >= 0 {
					_ = unix.Close(fd)
					s.devices[u.Slot] = -1
				}
				s.state.Units[i].Phase = 4
				if e = configureDNS(u); e != nil {
					s.lastFailure = "resolver-repair-pending"
				}
				if e = save(&s.state); e != nil {
					s.uncertain = true
					s.revoke()
					return
				}
			}
		}
	}
}
func serve() error {
	if e := securePath(Installed, false); e != nil {
		return e
	}
	lock, e := exclusive(State + "/service.lock")
	if e != nil {
		return e
	}
	defer func() { _ = unix.Close(lock) }()
	s := newService()
	state, exists, e := load()
	if e != nil {
		return e
	}
	if exists {
		s.state = state
		s.owned = true
		if e = s.cleanup(); e != nil {
			return e
		}
	}
	if st, e := os.Lstat(Socket); e == nil {
		if st.Mode()&os.ModeSocket == 0 || st.Sys().(*syscall.Stat_t).Uid != 0 {
			return errUncertain
		}
		if e = os.Remove(Socket); e != nil {
			return e
		}
	} else if !errors.Is(e, os.ErrNotExist) {
		return e
	}
	listener, e := net.ListenUnix("unix", &net.UnixAddr{Name: Socket, Net: "unix"})
	if e != nil {
		return e
	}
	listener.SetUnlinkOnClose(false)
	defer func() { _ = listener.Close(); _ = os.Remove(Socket) }()
	if e = os.Chmod(Socket, 0666); e != nil {
		return e
	}
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	done := make(chan struct{})
	go func() {
		ticker := time.NewTicker(20 * time.Millisecond)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				s.poll()
			case <-done:
				return
			}
		}
	}()
	defer close(done)
	go func() {
		<-signals
		s.mu.Lock()
		s.stopping = true
		s.revoke()
		_ = s.cleanup()
		s.mu.Unlock()
		_ = listener.Close()
	}()
	slots := make(chan struct{}, 8)
	for {
		c, e := listener.AcceptUnix()
		if e != nil {
			s.mu.Lock()
			stopping := s.stopping
			uncertainState := s.uncertain
			s.mu.Unlock()
			if stopping && !uncertainState {
				return nil
			}
			return e
		}
		select {
		case slots <- struct{}{}:
			go func() {
				defer func() { <-slots }()
				if !s.handle(c) {
					_ = c.Close()
				}
			}()
		default:
			_ = c.Close()
		}
	}
}
