//go:build darwin

package helper

import (
	"crypto/rand"
	"encoding/binary"
	"golang.org/x/sys/unix"
	"os"
	"os/signal"
	"stelvio.dev/traforo/darwin"
	"stelvio.dev/traforo/protocol"
	"syscall"
	"time"
)

const (
	OK uint16 = iota
	Busy
	Invalid
	Uncertain
	Unauthorized
)

type client struct {
	fd       int
	peer     darwin.Peer
	data     []byte
	deadline time.Time
}
type grant struct {
	active, failed bool
	phase          byte
	generation     uint64
	request        protocol.Request
	device         darwin.Device
}
type jobResult struct {
	kind       byte
	client     client
	request    protocol.Request
	state      Snapshot
	devices    [8]darwin.Device
	stage      byte
	err        error
	cacheError bool
}
type service struct {
	store                                       Store
	gate, listener, lease, carrier              int
	state                                       Snapshot
	devices                                     [8]darwin.Device
	grants                                      [8]grant
	clients                                     [8]client
	peer                                        darwin.Peer
	session, capability                         [16]byte
	owned, uncertain, working, revoke, stopping bool
	cleanupAfter, stopDeadline                  time.Time
	completion                                  chan jobResult
}

func initDevices() [8]darwin.Device {
	var d [8]darwin.Device
	for i := range d {
		d[i].FD = -1
	}
	return d
}
func reply(fd int, status uint16, body []byte, descriptor int) bool {
	if len(body) > 256 {
		return false
	}
	packet := make([]byte, 16+len(body))
	copy(packet, "STLVREP1")
	binary.BigEndian.PutUint16(packet[8:], status)
	binary.BigEndian.PutUint32(packet[12:], uint32(len(body)))
	copy(packet[16:], body)
	var control []byte
	if descriptor >= 0 {
		control = unix.UnixRights(descriptor)
	}
	n, e := unix.SendmsgN(fd, packet, control, nil, unix.MSG_DONTWAIT)
	return e == nil && n == len(packet)
}
func replyClose(fd int, status uint16) { reply(fd, status, nil, -1); unix.Close(fd) }
func (c *client) close() {
	if c.fd >= 0 {
		unix.Close(c.fd)
	}
	*c = client{fd: -1}
}
func (c *client) receive() (protocol.Request, bool, error) {
	target := protocol.Header
	if len(c.data) >= target {
		length := binary.BigEndian.Uint32(c.data[12:])
		if length > protocol.MaxBody {
			return protocol.Request{}, false, protocol.Invalid
		}
		target += int(length)
	}
	buf := make([]byte, target-len(c.data))
	if len(buf) > 0 {
		n, e := darwin.Receive(c.fd, buf)
		if e == unix.EAGAIN || e == unix.EINTR {
			return protocol.Request{}, false, nil
		}
		if e != nil || n <= 0 {
			return protocol.Request{}, false, uncertain
		}
		c.data = append(c.data, buf[:n]...)
	}
	if len(c.data) < protocol.Header {
		return protocol.Request{}, false, nil
	}
	length := binary.BigEndian.Uint32(c.data[12:])
	if length > protocol.MaxBody {
		return protocol.Request{}, false, protocol.Invalid
	}
	if len(c.data) != protocol.Header+int(length) {
		return protocol.Request{}, false, nil
	}
	r, e := protocol.Decode(c.data)
	return r, e == nil, e
}
func (s *service) revokeLease() {
	if s.lease >= 0 {
		unix.Close(s.lease)
	}
	if s.carrier >= 0 {
		unix.Close(s.carrier)
	}
	s.lease = -1
	s.carrier = -1
	for i := range s.grants {
		s.grants[i].active = false
	}
	s.revoke = true
}
func gate(shared bool) (int, error) {
	if !trusted() {
		return -1, uncertain
	}
	for _, p := range []string{darwin.StateParent, darwin.State} {
		fd, e := safeDirectory(p)
		if e != nil {
			return -1, e
		}
		unix.Close(fd)
	}
	return lockFile(darwin.State+"/admission", true, shared, false)
}
func (s *service) start(kind byte, c *client, r protocol.Request) bool {
	if s.working {
		return false
	}
	if s.gate < 0 {
		fd, e := gate(true)
		if e != nil {
			return false
		}
		s.gate = fd
	}
	j := jobResult{kind: kind, request: r, state: s.state.Clone(), devices: s.devices, client: client{fd: -1}}
	if c != nil {
		j.client = *c
		*c = client{fd: -1}
	}
	for i := range s.grants {
		if kind != protocol.Configure && kind != protocol.Remove || s.grants[i].request.Unit == r.Unit {
			s.grants[i].active = false
		}
	}
	s.working = true
	go func() {
		switch kind {
		case protocol.Configure:
			j.err = configureUnit(s.store, &j.state, &j.devices, j.client.data, &j.stage)
		case protocol.Remove:
			j.err = removeUnit(s.store, &j.state, &j.devices, r.Unit, r.Generation, r.KeepDNS)
		default:
			j.err = cleanup(s.store, &j.state, &j.devices)
		}
		if j.err != nil {
			current, exists, e := s.store.Load()
			if e != nil || !exists {
				j.cacheError = true
			} else {
				j.state = current
			}
		}
		s.completion <- j
	}()
	return true
}
func (s *service) refresh() {
	for i := range s.grants {
		s.grants[i].active = false
		s.grants[i].phase = 0
	}
	for i, u := range s.state.Units {
		g := &s.grants[i]
		g.phase = u.Phase
		g.generation = u.Generation
		g.request = u.Request
		g.device = s.devices[i]
		g.active = !s.revoke && s.lease >= 0 && !s.uncertain && !g.failed && u.Phase == Active && g.device.Current()
	}
}
func (s *service) acquire(c *client, r protocol.Request) {
	if !darwin.Alive(c.peer) {
		replyClose(c.fd, Uncertain)
		c.fd = -1
		return
	}
	pair, e := darwin.Pair()
	if e != nil {
		replyClose(c.fd, Uncertain)
		c.fd = -1
		return
	}
	defer unix.Close(pair[1])
	if s.gate < 0 {
		s.gate, e = gate(true)
		if e != nil {
			unix.Close(pair[0])
			replyClose(c.fd, Busy)
			c.fd = -1
			return
		}
	}
	state := Snapshot{Revision: 1, Carrier: 1, Peer: c.peer, Session: r.Session}
	if _, e = rand.Read(state.Capability[:]); e != nil {
		unix.Close(pair[0])
		replyClose(c.fd, Uncertain)
		c.fd = -1
		return
	}
	if e = s.store.Save(state); e != nil {
		unix.Close(pair[0])
		s.uncertain = true
		if current, exists, e := s.store.Load(); e == nil && exists {
			s.state = current
			s.owned = true
			s.peer = current.Peer
			s.session = current.Session
			s.capability = current.Capability
		}
		replyClose(c.fd, Uncertain)
		c.fd = -1
		return
	}
	s.state = state
	s.owned = true
	s.uncertain = false
	s.revoke = false
	s.peer = state.Peer
	s.session = state.Session
	s.capability = state.Capability
	s.carrier = pair[0]
	s.grants = [8]grant{}
	body := make([]byte, 20)
	copy(body, state.Capability[:])
	binary.BigEndian.PutUint32(body[16:], 1)
	delivered := reply(c.fd, OK, body, pair[1])
	s.lease = c.fd
	c.fd = -1
	if !delivered {
		s.revokeLease()
	}
}
func (s *service) complete(j jobResult) {
	s.working = false
	s.state = j.state
	s.devices = j.devices
	disposal := j.kind != protocol.Configure && j.kind != protocol.Remove
	if j.cacheError {
		s.uncertain = true
		s.revokeLease()
	}
	if !disposal {
		for i, u := range s.state.Units {
			if u.Request.Unit == j.request.Unit {
				s.grants[i].failed = j.err != nil
			}
		}
	}
	if disposal && j.err == nil {
		s.owned = false
		s.uncertain = false
		s.peer = darwin.Peer{}
		s.session = [16]byte{}
		s.capability = [16]byte{}
		if s.gate >= 0 {
			unix.Close(s.gate)
			s.gate = -1
		}
	} else if disposal {
		s.uncertain = true
		s.cleanupAfter = time.Now().Add(time.Second)
	}
	if j.client.fd >= 0 {
		if j.kind == protocol.Acquire && j.err == nil && !s.stopping && !s.revoke {
			peer, e := darwin.ReadPeer(j.client.fd)
			if e == nil && peer == j.client.peer {
				s.acquire(&j.client, j.request)
			} else {
				replyClose(j.client.fd, Unauthorized)
			}
		} else if j.kind == protocol.Configure && j.err != nil && j.stage != 0 {
			reply(j.client.fd, Uncertain, []byte{'C', 'F', 1, j.stage}, -1)
			unix.Close(j.client.fd)
		} else {
			status := OK
			if j.err != nil {
				status = Uncertain
			}
			replyClose(j.client.fd, status)
		}
	}
	s.refresh()
}
func (s *service) validChange(c client, r protocol.Request) bool {
	return validChange(s.state, c.data, r)
}
func (s *service) handle(c *client, r protocol.Request) {
	reject := func(status uint16) { replyClose(c.fd, status); c.fd = -1 }
	if r.Operation == protocol.Inspect {
		body := make([]byte, 21)
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
		if s.working {
			flags |= 4
		}
		binary.BigEndian.PutUint32(body[16:], flags)
		for _, g := range s.grants {
			if g.phase == 0 {
				continue
			}
			body[20]++
			unit := make([]byte, 13)
			binary.BigEndian.PutUint32(unit, g.request.Unit)
			binary.BigEndian.PutUint64(unit[4:], g.generation)
			switch {
			case g.active:
				unit[12] = 1
			case g.failed:
				unit[12] = 2
			case g.phase == Preparing || g.phase == Removing:
				unit[12] = 3
			case g.phase == Retained:
				unit[12] = 4
			}
			body = append(body, unit...)
		}
		reply(c.fd, OK, body, -1)
		c.close()
		return
	}
	if s.working || s.stopping {
		reject(Busy)
		return
	}
	if r.Operation == protocol.Acquire {
		if s.lease >= 0 || s.owned && darwin.Alive(s.peer) && (c.peer != s.peer || r.Session != s.session) {
			reject(Busy)
			return
		}
		if s.owned || s.uncertain {
			s.revoke = false
			if !s.start(protocol.Acquire, c, r) {
				c.close()
			}
		} else {
			s.acquire(c, r)
		}
		if c.fd < 0 {
			*c = client{fd: -1}
		}
		return
	}
	if r.Operation == protocol.Reconcile {
		if s.lease >= 0 || s.owned && darwin.Alive(s.peer) {
			reject(Busy)
			return
		}
		if !s.start(protocol.Reconcile, c, r) {
			c.close()
		}
		return
	}
	if !s.owned || s.lease < 0 || s.revoke || !authorized(s.state, c.peer, r) {
		reject(Unauthorized)
		return
	}
	if !s.validChange(*c, r) {
		reject(Invalid)
		return
	}
	if r.Operation == protocol.Release {
		s.revokeLease()
	}
	if !s.start(r.Operation, c, r) {
		c.close()
	}
}
func (s *service) accept() {
	fd, _, e := unix.Accept(s.listener)
	if e != nil {
		return
	}
	if darwin.Nonblocking(fd) != nil {
		unix.Close(fd)
		return
	}
	peer, e := darwin.ReadPeer(fd)
	if e != nil {
		unix.Close(fd)
		return
	}
	if s.owned && (s.lease >= 0 || darwin.Alive(s.peer)) && peer != s.peer {
		replyClose(fd, Busy)
		return
	}
	for i := range s.clients {
		if s.clients[i].fd < 0 {
			s.clients[i] = client{fd: fd, peer: peer, deadline: time.Now().Add(10 * time.Second)}
			return
		}
	}
	replyClose(fd, Busy)
}
func (s *service) pumpReturn() {
	buf := make([]byte, 1517)
	n, e := darwin.Receive(s.carrier, buf)
	if e == unix.EAGAIN || e == unix.EINTR {
		return
	}
	if e != nil {
		s.revokeLease()
		return
	}
	if _, frameError := protocol.DecodeFrame(buf[:n]); frameError != nil || binary.BigEndian.Uint32(buf[12:]) != protocol.IPv4 {
		return
	}
	id, generation := binary.BigEndian.Uint32(buf), binary.BigEndian.Uint64(buf[4:])
	for i, g := range s.grants {
		if !g.active || g.request.Unit != id {
			continue
		}
		if generation != g.generation || !protocol.PacketAllowed(buf[16:n], g.request, uint32(0xc0000201+2*i), false) {
			return
		}
		if !g.device.Current() {
			s.grants[i].active = false
			s.grants[i].failed = true
			return
		}
		packet := buf[12:n]
		n, e = unix.SendmsgN(g.device.FD, packet, nil, nil, unix.MSG_DONTWAIT)
		if e == unix.EAGAIN || e == unix.EINTR {
			return
		}
		if e != nil || n != len(packet) {
			s.grants[i].active = false
			s.grants[i].failed = true
		}
		return
	}
}
func (s *service) pumpHost(i int) {
	g := s.grants[i]
	if !g.device.Current() {
		s.grants[i].active = false
		s.grants[i].failed = true
		return
	}
	buf := make([]byte, 1505)
	n, e := darwin.Receive(g.device.FD, buf)
	if e == unix.EAGAIN || e == unix.EINTR {
		return
	}
	if e != nil || n > 1504 {
		s.grants[i].active = false
		s.grants[i].failed = true
		return
	}
	if n < 24 || binary.BigEndian.Uint32(buf) != protocol.IPv4 || !protocol.PacketAllowed(buf[4:n], g.request, uint32(0xc0000201+2*i), true) {
		return
	}
	wire := make([]byte, 12+n)
	binary.BigEndian.PutUint32(wire, g.request.Unit)
	binary.BigEndian.PutUint64(wire[4:], g.generation)
	copy(wire[12:], buf[:n])
	sent, e := unix.SendmsgN(s.carrier, wire, nil, nil, unix.MSG_DONTWAIT)
	if e == unix.EAGAIN || e == unix.EINTR {
		return
	}
	if e != nil || sent != len(wire) {
		s.grants[i].active = false
		s.grants[i].failed = true
	}
}
func serve() error {
	if !trusted() || darwin.Supported() != nil {
		return uncertain
	}
	unix.Umask(077)
	image, e := lockFile(darwin.Installed, false, false, false)
	if e != nil {
		return e
	}
	defer unix.Close(image)
	lease, e := lockFile(darwin.State+"/lease", true, false, true)
	if e != nil {
		return e
	}
	defer unix.Close(lease)
	listener, e := darwin.Listener()
	if e != nil {
		return e
	}
	defer unix.Close(listener)
	s := service{store: Store{image, lease}, gate: -1, listener: listener, lease: -1, carrier: -1, devices: initDevices(), completion: make(chan jobResult, 1)}
	for i := range s.clients {
		s.clients[i].fd = -1
	}
	defer func() {
		s.revokeLease()
		for i := range s.clients {
			s.clients[i].close()
		}
		for i := range s.devices {
			s.devices[i].Close()
		}
		if s.gate >= 0 {
			unix.Close(s.gate)
		}
	}()
	s.gate, e = gate(true)
	if e != nil {
		return e
	}
	state, exists, e := s.store.Load()
	s.state = state
	s.uncertain = e != nil
	if exists {
		s.owned = true
		s.peer = state.Peer
		s.session = state.Session
		s.capability = state.Capability
	} else if e == nil {
		unix.Close(s.gate)
		s.gate = -1
	}
	s.refresh()
	signals := make(chan os.Signal, 1)
	signal.Notify(signals, syscall.SIGTERM, syscall.SIGINT)
	defer signal.Stop(signals)
	for {
		select {
		case <-signals:
			if !s.stopping {
				s.stopping = true
				s.stopDeadline = time.Now().Add(30 * time.Second)
				s.revokeLease()
				for i := range s.clients {
					s.clients[i].close()
				}
			}
		default:
		}
		select {
		case j := <-s.completion:
			s.complete(j)
		default:
		}
		if s.stopping && time.Now().After(s.stopDeadline) {
			if s.working {
				os.Exit(1)
			} // Kernel closes FDs; preserve durable recovery evidence.
			return uncertain
		}
		if s.lease >= 0 && !darwin.Alive(s.peer) {
			s.revokeLease()
		}
		if !s.working && (s.revoke || s.owned && !darwin.Alive(s.peer) && !time.Now().Before(s.cleanupAfter)) {
			if !s.start(protocol.Reconcile, nil, protocol.Request{}) {
				return uncertain
			}
			s.revoke = false
		}
		if s.stopping && !s.working && !s.owned && !s.uncertain {
			return nil
		}
		fds := make([]unix.PollFd, 3+8+8)
		fds[0] = unix.PollFd{Fd: int32(s.listener), Events: unix.POLLIN}
		if s.stopping {
			fds[0].Fd = -1
		}
		fds[1] = unix.PollFd{Fd: int32(s.lease), Events: unix.POLLIN}
		fds[2] = unix.PollFd{Fd: int32(s.carrier), Events: unix.POLLIN}
		for i, c := range s.clients {
			fds[3+i] = unix.PollFd{Fd: int32(c.fd), Events: unix.POLLIN}
		}
		for i, g := range s.grants {
			fd := -1
			if g.active {
				fd = g.device.FD
			}
			fds[11+i] = unix.PollFd{Fd: int32(fd), Events: unix.POLLIN}
		}
		_, e = unix.Poll(fds, 50)
		if e == unix.EINTR {
			continue
		}
		if e != nil {
			return e
		}
		if s.lease >= 0 && fds[1].Revents != 0 {
			_, e := darwin.Receive(s.lease, make([]byte, 1))
			if e != unix.EAGAIN && e != unix.EINTR {
				s.revokeLease()
			}
		}
		if fds[0].Revents != 0 && !s.stopping {
			s.accept()
		}
		for i := range s.clients {
			c := &s.clients[i]
			if c.fd < 0 {
				continue
			}
			if time.Now().After(c.deadline) {
				c.close()
				continue
			}
			if fds[3+i].Fd != int32(c.fd) || fds[3+i].Revents == 0 {
				continue
			}
			r, complete, e := c.receive()
			if e != nil {
				c.close()
			} else if complete {
				if time.Now().After(c.deadline) {
					c.close()
				} else {
					s.handle(c, r)
				}
			}
		}
		if s.carrier >= 0 && fds[2].Revents != 0 {
			s.pumpReturn()
		}
		for i, g := range s.grants {
			if g.active && fds[11+i].Fd == int32(g.device.FD) && fds[11+i].Revents != 0 {
				s.pumpHost(i)
			}
		}
	}
}
