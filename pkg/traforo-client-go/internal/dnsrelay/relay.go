// Package dnsrelay provides scoped, generation-fenced private DNS outside root.
package dnsrelay

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"strings"
	"sync"
	"syscall"
	"time"

	"codeberg.org/miekg/dns"
	"codeberg.org/miekg/dns/dnsutil"
	"github.com/stelviodev/traforo/internal/protocol"
	"github.com/xjasonlyu/tun2socks/v2/transport/socks5"
)

const Timeout = 3 * time.Second
const MaxRequests = 32
const MaxUDP = 4096

var ErrStale = errors.New("stale DNS revision")
var ErrUnavailable = errors.New("private DNS unavailable")
var ErrAuthorization = errors.New("DNS resource outside routed destination ranges")

type Config struct {
	Revision           uint64
	Domains, Hostnames []string
	// Resolver is supplied by the AWS ownership-validated session, not a routed resource.
	Resolver netip.Addr
	Proxy    string
	// CIDRs authorizes resource destinations; it need not contain Resolver.
	CIDRs []netip.Prefix
}

func (c Config) Validate() error {
	p, err := netip.ParseAddrPort(c.Proxy)
	if err != nil || p.Addr() != netip.MustParseAddr("127.0.0.1") || p.Port() < 1024 || c.Revision == 0 {
		return errors.New("invalid DNS configuration")
	}
	if len(c.CIDRs) == 0 || len(c.CIDRs) > protocol.MaxRanges {
		return errors.New("invalid DNS CIDRs")
	}
	for i, prefix := range c.CIDRs {
		if !prefix.IsValid() || !prefix.Addr().Is4() || prefix != prefix.Masked() {
			return errors.New("invalid DNS CIDR")
		}
		if _, err := protocol.Prefix(prefix.String()); err != nil {
			return errors.New("invalid DNS CIDR")
		}
		for _, prior := range c.CIDRs[:i] {
			if prefix.Overlaps(prior) {
				return errors.New("overlapping DNS CIDRs")
			}
		}
	}
	if !c.Resolver.Is4() || !c.Resolver.IsPrivate() {
		return errors.New("DNS resolver must be an RFC1918 IPv4 address")
	}
	if len(c.Domains)+len(c.Hostnames) > protocol.MaxDomains {
		return errors.New("too many DNS names")
	}
	for _, names := range [][]string{c.Domains, c.Hostnames} {
		seen := make(map[string]struct{}, len(names))
		for _, name := range names {
			normalized, err := protocol.Domain(name)
			if err != nil || normalized != name {
				return errors.New("noncanonical DNS name")
			}
			if _, err := netip.ParseAddr(name); err == nil {
				return errors.New("DNS names cannot be IP addresses")
			}
			if _, exists := seen[name]; exists {
				return errors.New("duplicate DNS name")
			}
			seen[name] = struct{}{}
		}
	}
	return nil
}
func (c Config) ownsAddress(ip netip.Addr) bool {
	if !ip.Is4() {
		return false
	}
	for _, p := range c.CIDRs {
		if p.Contains(ip) {
			return true
		}
	}
	return false
}
func (c Config) owns(name string) bool {
	for _, h := range c.Hostnames {
		if name == h {
			return true
		}
	}
	for _, d := range c.Domains {
		if name == d || strings.HasSuffix(name, "."+d) {
			return true
		}
	}
	return false
}

type view struct {
	config Config
	ctx    context.Context
	cancel context.CancelFunc
	// DialContext is an internal IO boundary; numeric destinations are validated above it.
	dialContext func(context.Context, string, string) (net.Conn, error)
}
type observation struct {
	revision uint64
	seen     bool
}

type Relay struct {
	metricsOnce  sync.Once
	metrics      *Metrics
	mu           sync.Mutex
	current      *view
	closed       bool
	closeDone    chan struct{}
	observations map[string]observation
	udp          net.PacketConn
	tcp          net.Listener
	connections  map[net.Conn]struct{}
	workers      sync.WaitGroup
	slots        chan struct{}
	tcpSlots     chan struct{}
}

func New(c Config) (*Relay, error) {
	return NewWithMetrics(c, nil)
}

// NewWithMetrics retains a logical unit counter identity across listeners.
func NewWithMetrics(c Config, metrics *Metrics) (*Relay, error) {
	if metrics == nil {
		metrics = &Metrics{}
	}
	if err := c.Validate(); err != nil {
		return nil, err
	}
	udp, tcp, err := listenPair(
		func() (net.PacketConn, error) { return net.ListenPacket("udp4", "127.0.0.1:0") },
		func(address string) (net.Listener, error) { return net.Listen("tcp4", address) },
	)
	if err != nil {
		return nil, err
	}
	r := &Relay{metrics: metrics, udp: udp, tcp: tcp, closeDone: make(chan struct{}), observations: make(map[string]observation), connections: make(map[net.Conn]struct{}), slots: make(chan struct{}, MaxRequests), tcpSlots: make(chan struct{}, MaxRequests)}
	if err = r.Activate(c); err != nil {
		_ = udp.Close()
		_ = tcp.Close()
		return nil, err
	}
	r.workers.Add(2)
	go r.serveUDP()
	go r.serveTCP()
	return r, nil
}

// UDP and TCP allocate ephemeral ports independently. A UDP-selected port can
// already have a TCP listener. Retry only that collision, with a fixed bound.
func listenPair(udpListen func() (net.PacketConn, error), tcpListen func(string) (net.Listener, error)) (net.PacketConn, net.Listener, error) {
	for range 8 {
		udp, err := udpListen()
		if err != nil {
			return nil, nil, err
		}
		tcp, err := tcpListen(udp.LocalAddr().String())
		if err == nil {
			return udp, tcp, nil
		}
		_ = udp.Close()
		if !errors.Is(err, syscall.EADDRINUSE) {
			return nil, nil, err
		}
	}
	return nil, nil, fmt.Errorf("DNS listener pair: %w", syscall.EADDRINUSE)
}
func (r *Relay) Port() uint16 { return uint16(r.udp.LocalAddr().(*net.UDPAddr).Port) }
func (r *Relay) Activate(c Config) error {
	if err := c.Validate(); err != nil {
		return err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed || r.current != nil && c.Revision <= r.current.config.Revision {
		return ErrStale
	}
	if r.current != nil {
		r.current.cancel()
	}
	c.Domains = append([]string{}, c.Domains...)
	c.Hostnames = append([]string{}, c.Hostnames...)
	c.CIDRs = append([]netip.Prefix{}, c.CIDRs...)
	ctx, cancel := context.WithCancel(context.Background())
	r.current = &view{config: c, ctx: ctx, cancel: cancel}
	r.observations = make(map[string]observation)
	return nil
}
func (r *Relay) Reject(revision uint64) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed || r.current == nil || r.current.config.Revision != revision {
		return ErrStale
	}
	r.current.cancel()
	return nil
}
func (r *Relay) Observe(operation, name string, revision uint64) (bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	canonical, err := protocol.Domain(name)
	if err != nil || canonical != name || r.closed || r.current == nil || revision != r.current.config.Revision || r.current.ctx.Err() != nil {
		return false, ErrStale
	}
	o, exists := r.observations[name]
	switch operation {
	case "arm":
		if !r.current.config.owns(name) || exists || len(r.observations) >= MaxRequests {
			return false, errors.New("DNS observation bound")
		}
		r.observations[name] = observation{revision: revision}
	case "check", "release":
		if !exists || o.revision != revision {
			return false, ErrStale
		}
		if operation == "release" {
			delete(r.observations, name)
		}
		return o.seen, nil
	default:
		return false, errors.New("invalid DNS observation")
	}
	return false, nil
}

func (r *Relay) snapshot(revision uint64) (*view, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	v := r.current
	if r.closed || v == nil || v.config.Revision != revision || v.ctx.Err() != nil {
		return nil, ErrUnavailable
	}
	return v, nil
}

// dial authorizes a resource destination, including for ordinary-host probes.
func dial(v *view, address netip.AddrPort, direct bool) (net.Conn, error) {
	if !v.config.ownsAddress(address.Addr()) {
		return nil, ErrAuthorization
	}
	return connect(v, address, direct)
}

// connect receives only an authorized resource or the validated resolver endpoint.
// It never resolves a hostname through the host resolver. Cancellation closes IO.
func connect(v *view, address netip.AddrPort, direct bool) (net.Conn, error) {
	ctx, cancel := context.WithTimeout(v.ctx, Timeout)
	target := v.config.Proxy
	if direct {
		target = address.String()
	}
	open := v.dialContext
	if open == nil {
		open = (&net.Dialer{}).DialContext
	}
	c, err := open(ctx, "tcp4", target)
	if err != nil {
		cancel()
		return nil, err
	}
	stop := context.AfterFunc(ctx, func() { _ = c.Close() })
	deadline, _ := ctx.Deadline()
	_ = c.SetDeadline(deadline)
	if !direct {
		_, err = socks5.ClientHandshake(c, socks5.ParseAddrString(address.String()), socks5.CmdConnect, nil)
	}
	if err != nil {
		stop()
		cancel()
		_ = c.Close()
		return nil, err
	}
	return &deadlineConn{Conn: c, finish: func() { stop(); cancel() }}, nil
}

type deadlineConn struct {
	net.Conn
	finish func()
}

func (c *deadlineConn) Close() error { c.finish(); return c.Conn.Close() }

func exchange(v *view, q *dns.Msg) (*dns.Msg, error) {
	// The resolver may be outside routed subnets. Never send resolver traffic
	// through ordinary host routing or grant resource access to its address.
	c, err := connect(v, netip.AddrPortFrom(v.config.Resolver, 53), false)
	if err != nil {
		return nil, err
	}
	defer func() { _ = c.Close() }()
	a, _, err := dns.NewClient().ExchangeWithConn(v.ctx, q, c)
	if err != nil {
		return nil, err
	}
	if !a.Response || a.ID != q.ID || a.Opcode != q.Opcode || len(a.Question) != len(q.Question) || a.Truncated {
		return nil, ErrUnavailable
	}
	for i, x := range q.Question {
		y := a.Question[i]
		if !strings.EqualFold(x.Header().Name, y.Header().Name) || dns.RRToType(x) != dns.RRToType(y) || x.Header().Class != y.Header().Class {
			return nil, ErrUnavailable
		}
	}
	return a, nil
}

func (r *Relay) Answer(wire []byte, udp bool) ([]byte, error) {
	return r.answer(wire, udp, true)
}

func (r *Relay) answer(wire []byte, udp, upstream bool) ([]byte, error) {
	r.metricsOnce.Do(func() {
		if r.metrics == nil {
			r.metrics = &Metrics{}
		}
	})
	if udp {
		r.metrics.UDP.Add(1)
	} else {
		r.metrics.TCP.Add(1)
	}
	r.metrics.Active.Add(1)
	defer r.metrics.Active.Add(^uint64(0))
	if !upstream {
		r.metrics.Overload.Add(1)
	}
	q := &dns.Msg{Data: wire}
	if len(wire) > 65535 || udp && len(wire) > MaxUDP || q.Unpack() != nil || q.Response {
		return nil, errors.New("malformed DNS request")
	}
	a := new(dns.Msg)
	dnsutil.SetReply(a, q)
	a.Rcode = dns.RcodeServerFailure
	limit := 65535
	if udp {
		limit = 512
		if q.UDPSize > 0 {
			limit = int(q.UDPSize)
			if limit < 512 {
				limit = 512
			}
			if limit > MaxUDP {
				limit = MaxUDP
			}
		}
	}
	if q.Opcode != dns.OpcodeQuery || len(q.Question) != 1 {
		a.Rcode = dns.RcodeRefused
	} else {
		name, err := canonicalQueryName(q.Question[0].Header().Name)
		r.mu.Lock()
		v := r.current
		if o, ok := r.observations[name]; ok && v != nil && o.revision == v.config.Revision {
			o.seen = true
			r.observations[name] = o
		}
		available := !r.closed && v != nil && v.ctx.Err() == nil
		r.mu.Unlock()
		if err != nil || v == nil || !v.config.owns(name) {
			a.Rcode = dns.RcodeRefused
		} else if available && upstream {
			select {
			case r.slots <- struct{}{}:
				started := time.Now()
				upstream, e := exchange(v, q)
				r.metrics.duration(uint64(time.Since(started)))
				if e != nil {
					r.metrics.Failures.Add(1)
					var timeout net.Error
					if errors.As(e, &timeout) && timeout.Timeout() {
						r.metrics.Timeouts.Add(1)
					}
				}
				<-r.slots
				r.mu.Lock()
				if e == nil && r.current == v && !r.closed && v.ctx.Err() == nil {
					a = upstream
				} else if e == nil {
					r.metrics.Stale.Add(1)
				}
				r.mu.Unlock()
			default:
				r.metrics.Overload.Add(1)
			}
		}
	}
	if int(a.Rcode) < len(r.metrics.Responses) {
		r.metrics.Responses[a.Rcode].Add(1)
	}
	if err := packWithinLimit(a, limit); err != nil {
		return nil, err
	}
	return append([]byte(nil), a.Data...), nil
}

// packWithinLimit preserves the longest response prefix that fits, removing
// additional, authority, and answer records from the tail when truncation is
// required. V2 stores packed bytes in Msg.Data and exposes coarse truncation via
// dnsutil.Truncate, so the relay trims from the tail to retain partial answers.
func packWithinLimit(m *dns.Msg, limit int) error {
	if err := m.Pack(); err != nil {
		return err
	}
	if len(m.Data) <= limit {
		return nil
	}
	m.Truncated = true
	for _, section := range []*[]dns.RR{&m.Extra, &m.Ns, &m.Answer, &m.Pseudo} {
		for len(*section) > 0 {
			*section = (*section)[:len(*section)-1]
			if err := m.Pack(); err != nil {
				return err
			}
			if len(m.Data) <= limit {
				return nil
			}
		}
	}
	// A Security bit or UDPSize can synthesize an OPT record after the pseudo
	// section is empty. The question fits within the protocol minimum of 512 B.
	m.UDPSize, m.Security = 0, false
	return m.Pack()
}

func readTCPMessage(r io.Reader) (*dns.Msg, error) {
	m := new(dns.Msg)
	if _, err := m.ReadFrom(r); err != nil {
		return nil, err
	}
	if err := m.Unpack(); err != nil {
		return nil, err
	}
	return m, nil
}

func writeTCPMessage(w io.Writer, m *dns.Msg) error {
	if len(m.Data) == 0 {
		if err := m.Pack(); err != nil {
			return err
		}
	}
	var prefix [2]byte
	binary.BigEndian.PutUint16(prefix[:], uint16(len(m.Data)))
	if _, err := w.Write(prefix[:]); err != nil {
		return err
	}
	_, err := w.Write(m.Data)
	return err
}

// Probe resolves through SOCKS and validates every resource A record against CIDRs.
// direct selects ordinary host routing for the resource TCP connection only; a
// resolver-only probe never establishes packet-forwarding readiness.
func (r *Relay) Probe(revision uint64, hostname string, port uint16, direct bool) error {
	v, err := r.snapshot(revision)
	if err != nil {
		return err
	}
	q := new(dns.Msg)
	if hostname == "" {
		dnsutil.SetQuestion(q, ".", dns.TypeSOA)
	} else {
		n, err := protocol.Domain(hostname)
		if err != nil || n != hostname || !v.config.owns(n) || port == 0 {
			return ErrAuthorization
		}
		dnsutil.SetQuestion(q, dnsutil.Fqdn(hostname), dns.TypeA)
	}
	a, err := exchange(v, q)
	if err != nil {
		return err
	}
	if _, err = r.snapshot(revision); err != nil {
		return err
	}
	if a.Rcode != dns.RcodeSuccess && (hostname != "" || a.Rcode != dns.RcodeNameError) {
		return ErrUnavailable
	}
	if hostname != "" {
		addresses := []netip.Addr{}
		for _, rr := range a.Answer {
			if record, ok := rr.(*dns.A); ok {
				ip := record.Addr
				if !ip.IsValid() || !v.config.ownsAddress(ip.Unmap()) {
					return ErrAuthorization
				}
				addresses = append(addresses, ip.Unmap())
			}
		}
		if len(addresses) == 0 {
			return ErrUnavailable
		}
		c, err := dial(v, netip.AddrPortFrom(addresses[0], port), direct)
		if err != nil {
			return err
		}
		_ = c.Close()
	}
	if _, err = r.snapshot(revision); err != nil {
		return err
	}
	return nil
}

func (r *Relay) serveUDP() {
	defer r.workers.Done()
	buffer := make([]byte, MaxUDP+1)
	slots := make(chan struct{}, MaxRequests)
	for {
		n, peer, err := r.udp.ReadFrom(buffer)
		if err != nil {
			return
		}
		if n > MaxUDP {
			continue
		}
		wire := append([]byte{}, buffer[:n]...)
		select {
		case slots <- struct{}{}:
			r.workers.Add(1)
			go func() {
				defer r.workers.Done()
				defer func() { <-slots }()
				if a, e := r.Answer(wire, true); e == nil {
					_, _ = r.udp.WriteTo(a, peer)
				}
			}()
		default:
			// Same validation and ownership policy, without upstream IO.
			if answer, err := r.answer(wire, true, false); err == nil {
				_, _ = r.udp.WriteTo(answer, peer)
			}
		}
	}
}
func (r *Relay) serveTCP() {
	defer r.workers.Done()
	for {
		c, err := r.tcp.Accept()
		if err != nil {
			return
		}
		r.mu.Lock()
		if r.closed {
			r.mu.Unlock()
			_ = c.Close()
			return
		}
		select {
		case r.tcpSlots <- struct{}{}:
		default:
			r.connections[c] = struct{}{}
			r.mu.Unlock()
			r.answerTCPOverload(c)
			r.mu.Lock()
			delete(r.connections, c)
			r.mu.Unlock()
			continue
		}
		r.connections[c] = struct{}{}
		r.workers.Add(1)
		r.mu.Unlock()
		go func() {
			defer r.workers.Done()
			defer func() { <-r.tcpSlots; _ = c.Close(); r.mu.Lock(); delete(r.connections, c); r.mu.Unlock() }()
			for i := 0; i < 8; i++ {
				_ = c.SetDeadline(time.Now().Add(Timeout))
				q, err := readTCPMessage(c)
				if err != nil {
					return
				}
				answer, err := r.Answer(q.Data, false)
				if err != nil {
					return
				}
				a := &dns.Msg{Data: answer}
				if a.Unpack() != nil {
					return
				}
				_ = c.SetDeadline(time.Now().Add(Timeout))
				if writeTCPMessage(c, a) != nil {
					return
				}
			}
		}()
	}
}

// answerTCPOverload consumes at most one request inline when all TCP workers
// are busy. It performs no upstream IO and cannot create another worker.
func (r *Relay) answerTCPOverload(c net.Conn) {
	defer func() { _ = c.Close() }()
	_ = c.SetDeadline(time.Now().Add(Timeout))
	q, err := readTCPMessage(c)
	if err != nil {
		return
	}
	answer, err := r.answer(q.Data, false, false)
	if err != nil {
		return
	}
	a := &dns.Msg{Data: answer}
	if err = a.Unpack(); err != nil {
		return
	}
	_ = c.SetDeadline(time.Now().Add(Timeout))
	_ = writeTCPMessage(c, a)
}

func (r *Relay) Close() {
	r.mu.Lock()
	if r.closed {
		done := r.closeDone
		r.mu.Unlock()
		<-done
		return
	}
	r.closed = true
	r.current.cancel()
	for c := range r.connections {
		_ = c.Close()
	}
	r.mu.Unlock()
	_ = r.udp.Close()
	_ = r.tcp.Close()
	r.workers.Wait()
	close(r.closeDone)
}

// DNS query names may contain service-discovery labels such as _mongodb._tcp.
// Configured ownership names remain stricter canonical hostnames.
func canonicalQueryName(name string) (string, error) {
	name = strings.TrimSuffix(strings.ToLower(name), ".")
	if len(name) == 0 || len(name) > 253 {
		return "", protocol.ErrInvalid
	}
	for _, label := range strings.Split(name, ".") {
		if len(label) == 0 || len(label) > 63 {
			return "", protocol.ErrInvalid
		}
		for _, b := range []byte(label) {
			if (b < 'a' || b > 'z') && (b < '0' || b > '9') && b != '-' && b != '_' {
				return "", protocol.ErrInvalid
			}
		}
	}
	return name, nil
}

// Keep wire framing in the DNS library, never in the host resolver.
var _ io.ReadWriteCloser = (*deadlineConn)(nil)
