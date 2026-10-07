// The helper retains kernel descriptors. This process receives only revocable
// Unix datagrams and implements independent, generation-fenced IPv4/TCP stacks.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"strconv"
	"sync"
	"time"

	"github.com/xjasonlyu/tun2socks/v2/core"
	"github.com/xjasonlyu/tun2socks/v2/core/adapter"
	"github.com/xjasonlyu/tun2socks/v2/core/device/iobased"
	"github.com/xjasonlyu/tun2socks/v2/transport/socks5"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

const frameHeader = 16 // unit32 + generation64 + AF_INET32, all network order
const mtu = 1500

type command struct {
	Operation  string   `json:"operation"`
	Unit       uint32   `json:"unit"`
	Generation uint64   `json:"generation"`
	Attempt    uint64   `json:"attempt"`
	Proxy      string   `json:"proxy"`
	CIDRs      []string `json:"cidrs"`
}

type unit struct {
	id          uint32
	generation  uint64
	proxy       string
	cidrs       []netip.Prefix
	packets     chan []byte
	ctx         context.Context
	cancel      context.CancelFunc
	carrier     *net.UnixConn
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
	binary.BigEndian.PutUint32(wire[12:], 2) // Darwin AF_INET
	copy(wire[frameHeader:], packet)
	// Bound a full carrier without sharing write deadlines across VPC workers.
	u.writer.Lock()
	defer u.writer.Unlock()
	u.carrier.SetWriteDeadline(time.Now().Add(100 * time.Millisecond))
	_, _, err := u.carrier.WriteMsgUnix(wire, nil, nil)
	if err != nil {
		return 0, err
	}
	return len(packet), nil
}
func (u *unit) accepts(packet []byte, returning bool) bool {
	if len(packet) < 40 || len(packet) > mtu || packet[0] != 0x45 || packet[9] != 6 ||
		int(binary.BigEndian.Uint16(packet[2:4])) != len(packet) || binary.BigEndian.Uint16(packet[6:8])&0x3fff != 0 {
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
		connection.Close()
		return false
	}
	u.connections[connection] = struct{}{}
	return true
}
func (u *unit) discard(connection net.Conn) {
	connection.Close()
	u.mu.Lock()
	delete(u.connections, connection)
	u.mu.Unlock()
}
func (u *unit) HandleUDP(connection adapter.UDPConn) { connection.Close() }
func (u *unit) HandleTCP(connection adapter.TCPConn) {
	u.mu.Lock()
	if u.ctx.Err() != nil {
		u.mu.Unlock()
		connection.Close()
		return
	}
	select {
	case u.slots <- struct{}{}:
		u.workers.Add(1)
	default:
		u.mu.Unlock()
		connection.Close()
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
		remote.SetDeadline(deadline)
		if _, err = socks5.ClientHandshake(remote, socks5.ParseAddrString(destination.String()), socks5.CmdConnect, nil); err != nil {
			cancel()
			return
		}
		cancel()
		remote.SetDeadline(time.Time{})
		done := make(chan struct{})
		var once sync.Once
		finish := func() {
			once.Do(func() {
				end := time.Now().Add(30 * time.Second)
				remote.SetDeadline(end)
				connection.SetDeadline(end)
			})
		}
		go func() {
			io.Copy(remote, connection)
			if half, ok := remote.(interface{ CloseWrite() error }); ok {
				half.CloseWrite()
			}
			finish()
			close(done)
		}()
		io.Copy(connection, remote)
		if half, ok := any(connection).(interface{ CloseWrite() error }); ok {
			half.CloseWrite()
		}
		finish()
		<-done
	}()
}
func (u *unit) close() {
	u.mu.Lock()
	u.cancel()
	for connection := range u.connections {
		connection.Close()
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
	mu          sync.Mutex
	writer      sync.Mutex
	units       map[uint32]*unit
	generations map[uint32]uint64
	carrier     *net.UnixConn
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
		} else if m.generations[c.Unit] != c.Attempt {
			return errors.New("stale remove")
		}
		return nil
	}
	if c.Operation != "add" || c.Attempt <= m.generations[c.Unit] || m.units[c.Unit] != nil || len(m.units) >= 8 {
		return errors.New("invalid add")
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
	u := &unit{id: c.Unit, generation: c.Generation, proxy: c.Proxy, cidrs: ranges, packets: make(chan []byte, 256), ctx: ctx, cancel: cancel, carrier: m.carrier, writer: &m.writer, connections: make(map[net.Conn]struct{}), slots: make(chan struct{}, 128)}
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
	return nil
}
func (m *manager) dispatch() {
	buffer := make([]byte, frameHeader+mtu+1)
	for {
		n, _, flags, _, err := m.carrier.ReadMsgUnix(buffer, nil)
		if err != nil {
			return
		}
		if flags != 0 || n < frameHeader+40 || n > frameHeader+mtu || binary.BigEndian.Uint32(buffer[12:]) != 2 {
			continue
		}
		m.mu.Lock()
		u := m.units[binary.BigEndian.Uint32(buffer)]
		if u != nil && u.generation == binary.BigEndian.Uint64(buffer[4:]) && u.accepts(buffer[frameHeader:n], false) {
			packet := append([]byte(nil), buffer[frameHeader:n]...)
			select {
			case u.packets <- packet:
			default:
			}
		}
		m.mu.Unlock()
	}
}
func run() error {
	if os.Geteuid() == 0 || os.Getuid() != os.Geteuid() {
		return errors.New("nonroot execution required")
	}
	if len(os.Args) == 2 && os.Args[1] == "--version" {
		fmt.Println("stelvio-forwarder/1")
		return nil
	}
	if len(os.Args) != 2 {
		return errors.New("inherited carrier required")
	}
	fd, err := strconv.Atoi(os.Args[1])
	if err != nil || fd < 3 {
		return errors.New("invalid carrier")
	}
	file := os.NewFile(uintptr(fd), "carrier")
	connection, err := net.FileConn(file)
	file.Close()
	if err != nil {
		return err
	}
	carrier, ok := connection.(*net.UnixConn)
	if !ok {
		connection.Close()
		return errors.New("invalid carrier")
	}
	defer carrier.Close()
	// A single bounded write deadline is process-wide. The kernel endpoint is a
	// datagram socket; dropping under pressure is preferable to wedging cleanup.
	m := &manager{units: make(map[uint32]*unit), generations: make(map[uint32]uint64), carrier: carrier}
	defer func() {
		carrier.Close()
		m.mu.Lock()
		defer m.mu.Unlock()
		for _, u := range m.units {
			u.close()
		}
	}()
	go m.dispatch()
	fmt.Println("READY")
	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 4096), 16384)
	for scanner.Scan() {
		var c command
		decoder := json.NewDecoder(bytes.NewReader(scanner.Bytes()))
		decoder.DisallowUnknownFields()
		err = decoder.Decode(&c)
		if err == nil {
			var rest any
			if decoder.Decode(&rest) != io.EOF {
				err = errors.New("trailing command")
			}
		}
		if err == nil {
			err = m.apply(c)
		}
		if err != nil {
			fmt.Println("REJECTED")
		} else {
			fmt.Println("OK")
		}
	}
	return scanner.Err()
}
func main() {
	if run() != nil {
		fmt.Fprintln(os.Stderr, "forwarder failed")
		os.Exit(1)
	}
}
