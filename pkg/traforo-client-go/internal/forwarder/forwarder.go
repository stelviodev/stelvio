// The helper retains kernel descriptors. This process receives only revocable
// Unix datagrams and implements independent, generation-fenced IPv4/TCP stacks.
package forwarder

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/netip"
	"sync"
	"time"

	"github.com/stelviodev/traforo/internal/protocol"

	"github.com/xjasonlyu/tun2socks/v2/core"
	"github.com/xjasonlyu/tun2socks/v2/core/adapter"
	"github.com/xjasonlyu/tun2socks/v2/core/device/iobased"
	"github.com/xjasonlyu/tun2socks/v2/transport/socks5"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

const frameHeader = 16 // unit32 + generation64 + AF_INET32, all network order
const mtu = 1500
const packetQueue = 256
const maxStreamsPerUnit = 128

// packetCarrier is the existing datagram write boundary. Tests substitute bounded
// queues without Darwin datagram ENOBUFS terminating the synthetic device.
type packetCarrier interface {
	SetWriteDeadline(time.Time) error
	WriteMsgUnix([]byte, []byte, *net.UnixAddr) (int, int, error)
}

type unit struct {
	id          uint32
	generation  uint64
	proxy       string
	cidrs       []netip.Prefix
	packets     chan []byte
	ctx         context.Context
	cancel      context.CancelFunc
	carrier     packetCarrier
	writer      *sync.Mutex
	stack       *stack.Stack
	endpoint    *iobased.Endpoint
	mu          sync.Mutex
	connections map[net.Conn]struct{}
	workers     sync.WaitGroup
	slots       chan struct{}
}

func (u *unit) Read(buffer []byte) (int, error) {
	select {
	case <-u.ctx.Done():
		return 0, io.EOF
	case packet := <-u.packets:
		return copy(buffer, packet), nil
	}
}
func (u *unit) Write(packet []byte) (int, error) {
	if u.ctx.Err() != nil {
		return 0, io.ErrClosedPipe
	}
	if !u.accepts(packet, true) {
		return 0, errors.New("invalid return packet")
	}
	wire := make([]byte, frameHeader+len(packet))
	binary.BigEndian.PutUint32(wire, u.id)
	binary.BigEndian.PutUint64(wire[4:], u.generation)
	binary.BigEndian.PutUint32(wire[12:], protocol.IPv4) // Carrier protocol marker
	copy(wire[frameHeader:], packet)
	// Bound a full carrier without sharing write deadlines across VPC workers.
	u.writer.Lock()
	defer u.writer.Unlock()
	if err := u.carrier.SetWriteDeadline(time.Now().Add(100 * time.Millisecond)); err != nil {
		return 0, err
	}
	n, _, err := u.carrier.WriteMsgUnix(wire, nil, nil)
	if err != nil {
		return 0, err
	}
	if n != len(wire) {
		return 0, io.ErrShortWrite
	}
	return len(packet), nil
}
func (u *unit) accepts(packet []byte, returning bool) bool {
	if len(packet) < 40 || len(packet) > mtu || packet[0] != 0x45 || packet[9] != 6 ||
		int(binary.BigEndian.Uint16(packet[2:4])) != len(packet) || binary.BigEndian.Uint16(packet[6:8])&0xbfff != 0 ||
		int(packet[32]>>4)*4 < 20 || int(packet[32]>>4)*4 > len(packet)-20 {
		return false
	}
	offset := 16
	if returning {
		offset = 12
	}
	address := netip.AddrFrom4([4]byte(packet[offset : offset+4]))
	for _, cidr := range u.cidrs {
		if cidr.Contains(address) {
			return true
		}
	}
	return false
}
func (u *unit) retain(connection net.Conn) bool {
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.ctx.Err() != nil {
		_ = connection.Close() // Best-effort close while refusing a new connection.
		return false
	}
	u.connections[connection] = struct{}{}
	return true
}
func (u *unit) discard(connection net.Conn) {
	_ = connection.Close() // Best-effort teardown after the connection leaves the unit.
	u.mu.Lock()
	delete(u.connections, connection)
	u.mu.Unlock()
}
func (u *unit) HandleUDP(connection adapter.UDPConn) {
	_ = connection.Close() // UDP is unsupported; close the rejected endpoint.
}
func (u *unit) HandleTCP(connection adapter.TCPConn) {
	u.mu.Lock()
	if u.ctx.Err() != nil {
		u.mu.Unlock()
		_ = connection.Close() // Best-effort close while refusing a new connection.
		return
	}
	select {
	case u.slots <- struct{}{}:
		u.workers.Add(1)
	default:
		u.mu.Unlock()
		_ = connection.Close() // Best-effort close after the worker limit is reached.
		return
	}
	u.mu.Unlock()
	go func() {
		defer u.workers.Done()
		defer func() { <-u.slots }()
		if !u.retain(connection) {
			return
		}
		defer u.discard(connection)
		destination, err := netip.ParseAddrPort(connection.LocalAddr().String())
		if err != nil || !destination.Addr().Is4() {
			return
		}
		owned := false
		for _, cidr := range u.cidrs {
			owned = owned || cidr.Contains(destination.Addr())
		}
		if !owned {
			return
		}
		ctx, cancel := context.WithTimeout(u.ctx, 10*time.Second)
		remote, err := (&net.Dialer{}).DialContext(ctx, "tcp4", u.proxy)
		if err != nil {
			cancel()
			return
		}
		if !u.retain(remote) {
			cancel()
			return
		}
		defer u.discard(remote)
		deadline, _ := ctx.Deadline()
		if err = remote.SetDeadline(deadline); err != nil {
			cancel()
			return
		}
		if _, err = socks5.ClientHandshake(remote, socks5.ParseAddrString(destination.String()), socks5.CmdConnect, nil); err != nil {
			cancel()
			return
		}
		cancel()
		if err = remote.SetDeadline(time.Time{}); err != nil {
			return
		}
		done := make(chan struct{})
		var once sync.Once
		var finishErr error
		finish := func() error {
			once.Do(func() {
				end := time.Now().Add(30 * time.Second)
				finishErr = errors.Join(remote.SetDeadline(end), connection.SetDeadline(end))
				if finishErr != nil {
					_ = remote.Close()
					_ = connection.Close()
				}
			})
			return finishErr
		}
		go func() {
			_, _ = io.Copy(remote, connection) // Stream termination is handled by half-close/deadline below.
			if half, ok := remote.(interface{ CloseWrite() error }); ok {
				_ = half.CloseWrite()
			}
			_ = finish()
			close(done)
		}()
		_, _ = io.Copy(connection, remote) // Stream termination is handled by half-close/deadline below.
		if half, ok := any(connection).(interface{ CloseWrite() error }); ok {
			_ = half.CloseWrite()
		}
		_ = finish()
		<-done
	}()
}
func (u *unit) close() {
	u.mu.Lock()
	u.cancel()
	for connection := range u.connections {
		_ = connection.Close() // Shutdown revokes all active streams before waiting.
	}
	u.mu.Unlock()
	if u.stack != nil {
		u.stack.Close()
	}
	if u.endpoint != nil {
		u.endpoint.Close()
		u.endpoint.Wait()
	}
	u.workers.Wait()
	if u.stack != nil {
		u.stack.Wait()
	}
}

type manager struct {
	mu              sync.Mutex
	writer          sync.Mutex
	units           map[uint32]*unit
	generations     map[uint32]uint64 // Last accepted transport attempt by unit.
	unitGenerations map[uint32]uint64 // Last helper generation; retained to fence old carrier frames.
	carrier         *net.UnixConn
	dns             map[uint32]*dnsUnit
}

func prefixes(values []string) ([]netip.Prefix, error) {
	if len(values) == 0 || len(values) > 8 {
		return nil, errors.New("invalid CIDR count")
	}
	private := []netip.Prefix{netip.MustParsePrefix("10.0.0.0/8"), netip.MustParsePrefix("172.16.0.0/12"), netip.MustParsePrefix("192.168.0.0/16")}
	result := []netip.Prefix{}
	for _, value := range values {
		prefix, err := netip.ParsePrefix(value)
		if err != nil || !prefix.Addr().Is4() || prefix != prefix.Masked() || prefix.String() != value || prefix.Bits() < 16 || prefix.Bits() > 28 {
			return nil, errors.New("invalid CIDR")
		}
		owned := false
		for _, parent := range private {
			owned = owned || parent.Contains(prefix.Addr())
		}
		if !owned {
			return nil, errors.New("nonprivate CIDR")
		}
		for _, existing := range result {
			if prefix.Overlaps(existing) {
				return nil, errors.New("overlapping CIDR")
			}
		}
		result = append(result, prefix)
	}
	return result, nil
}
func (m *manager) apply(c command) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.units == nil {
		m.units = make(map[uint32]*unit)
	}
	if m.generations == nil {
		m.generations = make(map[uint32]uint64)
	}
	if m.unitGenerations == nil {
		m.unitGenerations = make(map[uint32]uint64)
	}
	if c.Unit == 0 || c.Generation == 0 || c.Attempt == 0 {
		return errors.New("invalid generation identity")
	}
	if c.Operation == "remove" {
		if c.Proxy != "" || len(c.CIDRs) != 0 {
			return errors.New("invalid remove")
		}
		if current := m.units[c.Unit]; current != nil {
			if current.generation != c.Generation || m.generations[c.Unit] != c.Attempt {
				return errors.New("stale remove")
			}
			delete(m.units, c.Unit)
			current.close()
		} else if m.generations[c.Unit] != c.Attempt || m.unitGenerations[c.Unit] != c.Generation {
			return errors.New("stale remove")
		}
		return nil
	}
	if c.Operation != "add" || c.Attempt <= m.generations[c.Unit] || c.Generation <= m.unitGenerations[c.Unit] || m.units[c.Unit] != nil || len(m.units) >= protocol.MaxUnits {
		return errors.New("invalid add")
	}
	if m.unitGenerations[c.Unit] == 0 && len(m.unitGenerations) >= protocol.MaxUnits {
		return errors.New("unit identity bound")
	}
	proxy, err := netip.ParseAddrPort(c.Proxy)
	if err != nil || proxy.Addr() != netip.MustParseAddr("127.0.0.1") || proxy.Port() < 1024 {
		return errors.New("invalid proxy")
	}
	ranges, err := prefixes(c.CIDRs)
	if err != nil {
		return err
	}
	for _, current := range m.units {
		for _, a := range ranges {
			for _, b := range current.cidrs {
				if a.Overlaps(b) {
					return errors.New("overlapping units")
				}
			}
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	u := &unit{id: c.Unit, generation: c.Generation, proxy: c.Proxy, cidrs: ranges, packets: make(chan []byte, packetQueue), ctx: ctx, cancel: cancel, carrier: m.carrier, writer: &m.writer, connections: make(map[net.Conn]struct{}), slots: make(chan struct{}, maxStreamsPerUnit)}
	endpoint, err := iobased.New(u, mtu, 0)
	if err != nil {
		cancel()
		return err
	}
	u.endpoint = endpoint
	s, err := core.CreateStack(&core.Config{LinkEndpoint: endpoint, TransportHandler: u})
	if err != nil {
		u.close()
		return err
	}
	u.stack = s
	m.units[c.Unit] = u
	m.generations[c.Unit] = c.Attempt
	m.unitGenerations[c.Unit] = c.Generation
	return nil
}
func (m *manager) dispatch() {
	buffer := make([]byte, frameHeader+mtu+1)
	for {
		n, _, flags, _, err := m.carrier.ReadMsgUnix(buffer, nil)
		if err != nil {
			return
		}
		if flags != 0 {
			continue
		}
		frame, err := protocol.DecodeFrame(buffer[:n])
		if err != nil || len(frame.Packet) < 44 || binary.BigEndian.Uint32(frame.Packet[:4]) != protocol.IPv4 {
			continue
		}
		packet := frame.Packet[4:]
		if len(packet) < 40 || len(packet) > mtu {
			continue
		}
		m.mu.Lock()
		u := m.units[frame.Unit]
		if u != nil && u.generation == frame.Generation && u.accepts(packet, false) {
			packet := append([]byte(nil), packet...)
			select {
			case u.packets <- packet:
			default:
			}
		}
		m.mu.Unlock()
	}
}
func (m *manager) close() {
	m.mu.Lock()
	units := m.units
	m.units = make(map[uint32]*unit)
	m.mu.Unlock()
	for _, u := range units {
		u.close()
	}
	m.closeDNS()
}

func serve(carrier *net.UnixConn, input io.Reader, output io.Writer) error {
	m := &manager{
		units:           make(map[uint32]*unit),
		generations:     make(map[uint32]uint64),
		unitGenerations: make(map[uint32]uint64),
		carrier:         carrier,
	}
	dispatchDone := make(chan struct{})
	go func() {
		defer close(dispatchDone)
		m.dispatch()
	}()
	defer func() {
		_ = carrier.Close()
		m.close()
		<-dispatchDone
	}()
	return m.serveControl(input, output)
}
