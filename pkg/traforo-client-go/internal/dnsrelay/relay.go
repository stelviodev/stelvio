// Package dnsrelay provides scoped, generation-fenced private DNS outside root.
package dnsrelay

import (
	"context"
	"errors"
	"io"
	"net"
	"net/netip"
	"strings"
	"sync"
	"time"

	"github.com/miekg/dns"
	"github.com/stelviodev/traforo/internal/protocol"
	"github.com/xjasonlyu/tun2socks/v2/transport/socks5"
)

const Timeout = 3 * time.Second
const MaxRequests = 32
const MaxUDP = 4096

var ErrStale = errors.New("stale DNS revision")
var ErrUnavailable = errors.New("private DNS unavailable")
var ErrAuthorization = errors.New("DNS resource outside owning VPC")

type Config struct {
	Revision           uint64
	Domains, Hostnames []string
	Resolver           netip.Addr
	Proxy              string
	CIDRs              []netip.Prefix
}

func (c Config) Validate() error {
	p, err := netip.ParseAddrPort(c.Proxy)
	if err != nil || p.Addr() != netip.MustParseAddr("127.0.0.1") || p.Port() < 1024 || c.Revision == 0 || !c.ownsAddress(c.Resolver) {
		return errors.New("invalid DNS configuration")
	}
	if len(c.Domains)+len(c.Hostnames) > 64 {
		return errors.New("too many DNS names")
	}
	for _, name := range append(append([]string{}, c.Domains...), c.Hostnames...) {
		normalized, err := protocol.Domain(name)
		if err != nil || normalized != name {
			return errors.New("noncanonical DNS name")
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
	mu           sync.Mutex
	current      *view
	closed       bool
	observations map[string]observation
	udp          *net.UDPConn
	tcp          net.Listener
	connections  map[net.Conn]struct{}
	workers      sync.WaitGroup
	slots        chan struct{}
}

func New(c Config) (*Relay, error) {
	if err := c.Validate(); err != nil {
		return nil, err
	}
	udp, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		return nil, err
	}
	tcp, err := net.Listen("tcp4", udp.LocalAddr().String())
	if err != nil {
		_ = udp.Close()
		return nil, err
	}
	r := &Relay{udp: udp, tcp: tcp, observations: make(map[string]observation), connections: make(map[net.Conn]struct{}), slots: make(chan struct{}, MaxRequests)}
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
	if r.current.config.Revision != revision {
		return ErrStale
	}
	r.current.cancel()
	return nil
}
func (r *Relay) Observe(operation, name string, revision uint64) (bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	canonical, err := protocol.Domain(name)
	if err != nil || canonical != name || revision != r.current.config.Revision || r.closed {
		return false, ErrStale
	}
	o, exists := r.observations[name]
	switch operation {
	case "arm":
		if exists || len(r.observations) >= MaxRequests {
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
	if r.closed || v.config.Revision != revision || v.ctx.Err() != nil {
		return nil, ErrUnavailable
	}
	return v, nil
}

// Dial never resolves a hostname through the host resolver. Cancellation closes IO.
func dial(v *view, address netip.AddrPort, direct bool) (net.Conn, error) {
	if !v.config.ownsAddress(address.Addr()) {
		return nil, ErrAuthorization
	}
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

func exchange(v *view, q *dns.Msg, direct bool) (*dns.Msg, error) {
	c, err := dial(v, netip.AddrPortFrom(v.config.Resolver, 53), direct)
	if err != nil {
		return nil, err
	}
	defer func() { _ = c.Close() }()
	dc := &dns.Conn{Conn: c}
	if err = dc.WriteMsg(q); err != nil {
		return nil, err
	}
	a, err := dc.ReadMsg()
	if err != nil {
		return nil, err
	}
	if !a.Response || a.Id != q.Id || a.Opcode != q.Opcode || len(a.Question) != len(q.Question) || a.Truncated {
		return nil, ErrUnavailable
	}
	for i, x := range q.Question {
		y := a.Question[i]
		if !strings.EqualFold(x.Name, y.Name) || x.Qtype != y.Qtype || x.Qclass != y.Qclass {
			return nil, ErrUnavailable
		}
	}
	return a, nil
}

func (r *Relay) Answer(wire []byte, udp bool) ([]byte, error) {
	return r.answer(wire, udp, true)
}

func (r *Relay) answer(wire []byte, udp, upstream bool) ([]byte, error) {
	q := new(dns.Msg)
	if len(wire) > 65535 || q.Unpack(wire) != nil || q.Response {
		return nil, errors.New("malformed DNS request")
	}
	a := new(dns.Msg)
	a.SetRcode(q, dns.RcodeServerFailure)
	limit := 65535
	if udp {
		limit = 512
		if e := q.IsEdns0(); e != nil {
			limit = int(e.UDPSize())
			if limit < 512 {
				limit = 512
			}
			if limit > MaxUDP {
				limit = MaxUDP
			}
		}
	}
	if q.Opcode != dns.OpcodeQuery || len(q.Question) != 1 {
		a.SetRcode(q, dns.RcodeRefused)
	} else {
		name, err := protocol.Domain(q.Question[0].Name)
		r.mu.Lock()
		v := r.current
		if o, ok := r.observations[name]; ok && o.revision == v.config.Revision {
			o.seen = true
			r.observations[name] = o
		}
		available := !r.closed && v.ctx.Err() == nil
		r.mu.Unlock()
		if err != nil || !v.config.owns(name) {
			a.SetRcode(q, dns.RcodeRefused)
		} else if available && upstream {
			select {
			case r.slots <- struct{}{}:
				upstream, e := exchange(v, q, false)
				<-r.slots
				r.mu.Lock()
				if e == nil && r.current == v && !r.closed && v.ctx.Err() == nil {
					a = upstream
				}
				r.mu.Unlock()
			default:
			}
		}
	}
	a.Compress = true
	a.Truncate(limit)
	return a.Pack()
}

// Probe checks actual payloads through SOCKS or through ordinary host routing.
func (r *Relay) Probe(revision uint64, hostname string, port uint16, direct bool) error {
	v, err := r.snapshot(revision)
	if err != nil {
		return err
	}
	q := new(dns.Msg)
	if hostname == "" {
		q.SetQuestion(".", dns.TypeSOA)
	} else {
		n, err := protocol.Domain(hostname)
		if err != nil || n != hostname || !v.config.owns(n) || port == 0 {
			return ErrAuthorization
		}
		q.SetQuestion(dns.Fqdn(hostname), dns.TypeA)
	}
	a, err := exchange(v, q, direct)
	if err != nil {
		return err
	}
	if a.Rcode != dns.RcodeSuccess && (hostname != "" || a.Rcode != dns.RcodeNameError) {
		return ErrUnavailable
	}
	if hostname != "" {
		addresses := []netip.Addr{}
		for _, rr := range a.Answer {
			if record, ok := rr.(*dns.A); ok {
				ip, ok := netip.AddrFromSlice(record.A)
				if !ok || !v.config.ownsAddress(ip.Unmap()) {
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
		n, peer, err := r.udp.ReadFromUDP(buffer)
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
					_, _ = r.udp.WriteToUDP(a, peer)
				}
			}()
		default:
			// Same validation and ownership policy, without upstream IO.
			if answer, err := r.answer(wire, true, false); err == nil {
				_, _ = r.udp.WriteToUDP(answer, peer)
			}
		}
	}
}
func (r *Relay) serveTCP() {
	defer r.workers.Done()
	slots := make(chan struct{}, MaxRequests)
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
		case slots <- struct{}{}:
		default:
			r.mu.Unlock()
			_ = c.Close()
			continue
		}
		r.connections[c] = struct{}{}
		r.workers.Add(1)
		r.mu.Unlock()
		go func() {
			defer r.workers.Done()
			defer func() { <-slots; _ = c.Close(); r.mu.Lock(); delete(r.connections, c); r.mu.Unlock() }()
			_ = c.SetDeadline(time.Now().Add(Timeout))
			dc := &dns.Conn{Conn: c}
			for i := 0; i < 8; i++ {
				q, err := dc.ReadMsg()
				if err != nil {
					return
				}
				wire, err := q.Pack()
				if err != nil {
					return
				}
				answer, err := r.Answer(wire, false)
				if err != nil {
					return
				}
				a := new(dns.Msg)
				if a.Unpack(answer) != nil {
					return
				}
				if dc.WriteMsg(a) != nil {
					return
				}
			}
		}()
	}
}
func (r *Relay) Close() {
	r.mu.Lock()
	if r.closed {
		r.mu.Unlock()
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
}

// Keep wire framing in the DNS library, never in the host resolver.
var _ io.ReadWriteCloser = (*deadlineConn)(nil)
