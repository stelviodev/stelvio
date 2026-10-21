//go:build darwin

package forwarder

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/netip"
	"sync"
	"testing"
	"time"

	"github.com/miekg/dns"
	"gvisor.dev/gvisor/pkg/tcpip/stack"
)

func TestSubnetUnitBoundsIsolationAndReconnect(t *testing.T) {
	carrier, peer := pair(t)
	m := &manager{carrier: carrier}
	t.Cleanup(func() { m.close(); _ = carrier.Close(); _ = peer.Close() })
	add := command{Operation: "add", Unit: 1, Generation: 7, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: subnetRanges(254, 32)}
	if err := m.apply(add); err != nil {
		t.Fatal(err)
	}
	first := m.units[1]
	add.CIDRs[0] = "10.254.0.0/16"
	if first.cidrs[0].String() != "10.254.1.0/24" {
		t.Fatal("unit retained caller's mutable configuration")
	}
	second := command{Operation: "add", Unit: 2, Generation: 9, Attempt: 1, Proxy: "127.0.0.1:1081", CIDRs: subnetRanges(253, 20)}
	if err := m.apply(second); err != nil {
		t.Fatal(err)
	}
	healthy := m.units[2]
	for _, tc := range []command{
		{Operation: "add", Unit: 3, Generation: 1, Attempt: 1, Proxy: add.Proxy, CIDRs: subnetRanges(252, 33)},
		{Operation: "add", Unit: 3, Generation: 1, Attempt: 1, Proxy: add.Proxy, CIDRs: []string{"10.254.32.0/24"}},
		{Operation: "add", Unit: 1, Generation: 8, Attempt: 2, Proxy: add.Proxy, CIDRs: []string{"10.254.0.0/16"}},
	} {
		if err := m.apply(tc); err == nil {
			t.Fatal("invalid add accepted")
		}
		if m.units[1] != first || m.units[2] != healthy || m.unitGenerations[3] != 0 || m.generations[3] != 0 {
			t.Fatal("invalid add changed live state or consumed identity")
		}
	}
	if err := m.apply(command{Operation: "remove", Unit: 1, Generation: 7, Attempt: 1}); err != nil {
		t.Fatal(err)
	}
	if m.units[2] != healthy || healthy.ctx.Err() != nil {
		t.Fatal("removal affected independent unit")
	}
	add.CIDRs = subnetRanges(254, 32)
	add.Generation = 8
	add.Attempt = 2
	if err := m.apply(add); err != nil {
		t.Fatal(err)
	}
	current := m.units[1]
	for _, address := range []string{"10.254.1.3", "10.254.32.3"} {
		if !current.accepts(subnetPacket(address, false), false) {
			t.Fatal("reconnect lost selected subnet")
		}
	}
	for _, address := range []string{"10.254.0.2", "10.254.0.9", "10.254.33.3", "10.253.1.3"} {
		if current.accepts(subnetPacket(address, false), false) {
			t.Fatal("reconnect broadened destinations")
		}
	}
	if err := m.apply(command{Operation: "remove", Unit: 1, Generation: 7, Attempt: 1}); err == nil {
		t.Fatal("stale remove accepted")
	}
	if m.units[2] != healthy || healthy.ctx.Err() != nil {
		t.Fatal("reconnect affected independent unit")
	}
}

// This real SOCKS fixture observes numeric targets. It supplies a DNS answer and
// accepts resource TCP handshakes without changing host routes or resolver files.
func subnetProbeProxy(t *testing.T, address string) (string, <-chan string) {
	t.Helper()
	listener, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	targets := make(chan string, 8)
	done := make(chan struct{})
	var workers sync.WaitGroup
	go func() {
		defer close(done)
		for {
			c, err := listener.Accept()
			if err != nil {
				return
			}
			workers.Add(1)
			go func() {
				defer workers.Done()
				defer func() { _ = c.Close() }()
				_ = c.SetDeadline(time.Now().Add(3 * time.Second))
				greeting := make([]byte, 3)
				if _, err := io.ReadFull(c, greeting); err != nil {
					return
				}
				if string(greeting) != string([]byte{5, 1, 0}) {
					t.Error("unexpected SOCKS authentication")
					return
				}
				if _, err := c.Write([]byte{5, 0}); err != nil {
					return
				}
				request := make([]byte, 10)
				if _, err := io.ReadFull(c, request); err != nil {
					return
				}
				if string(request[:4]) != string([]byte{5, 1, 0, 1}) {
					t.Error("SOCKS destination was not numeric IPv4")
					return
				}
				target := fmt.Sprintf("%s:%d", net.IP(request[4:8]), binary.BigEndian.Uint16(request[8:]))
				targets <- target
				if _, err := c.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 0}); err != nil {
					return
				}
				if target != "10.254.0.2:53" {
					return
				}
				dc := &dns.Conn{Conn: c}
				q, err := dc.ReadMsg()
				if err != nil {
					return
				}
				a := new(dns.Msg)
				a.SetReply(q)
				a.Answer = []dns.RR{&dns.A{Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeA, Class: 1, Ttl: 17}, A: net.ParseIP(address).To4()}}
				_ = dc.WriteMsg(a)
			}()
		}
	}()
	t.Cleanup(func() { _ = listener.Close(); <-done; workers.Wait() })
	return listener.Addr().String(), targets
}

func TestDNSControlUsesExactPacketSubnetSet(t *testing.T) {
	for _, tc := range []struct {
		address string
		allowed bool
	}{
		{"10.254.1.3", true}, {"10.254.32.3", true},
		{"10.254.0.2", false}, {"10.254.0.9", false}, {"10.254.33.3", false}, {"10.253.1.3", false},
	} {
		t.Run(tc.address, func(t *testing.T) {
			proxy, targets := subnetProbeProxy(t, tc.address)
			carrier, peer := pair(t)
			m := &manager{carrier: carrier}
			t.Cleanup(func() { m.close(); _ = carrier.Close(); _ = peer.Close() })
			id := uint64(0)
			send := func(c command) reply {
				t.Helper()
				id++
				c.Version = 2
				c.ID = id
				wire, err := json.Marshal(c)
				if err != nil {
					t.Fatal(err)
				}
				parsed, err := decodeCommand(wire)
				if err != nil {
					t.Fatal(err)
				}
				return m.control(parsed)
			}
			if r := send(command{Operation: "add", Unit: 1, Generation: 7, Attempt: 1, Proxy: proxy, CIDRs: subnetRanges(254, 32)}); r.Status != "ok" {
				t.Fatal(r)
			}
			configure := command{Operation: "dns-configure", Unit: 1, Generation: 7, Attempt: 1, Revision: 1, Resolver: "10.254.0.2", Hostnames: []string{"db.private.test"}}
			r := send(configure)
			if r.Status != "ok" || r.Port < 1024 {
				t.Fatal("off-route resolver refused", r)
			}
			// DNS updates cannot substitute a parent VPC range for the packet ranges.
			configure.Revision = 2
			configure.CIDRs = []string{"10.254.0.0/16"}
			if r := send(configure); r.Status != "error" {
				t.Fatal("DNS configuration broadened packet ranges")
			}
			r = send(command{Operation: "dns-probe", Unit: 1, Generation: 7, Attempt: 1, Revision: 1, Name: "db.private.test", Port: 27017})
			if tc.allowed {
				if r.Status != "ok" {
					t.Fatal("selected subnet refused", r)
				}
			} else if r.Status != "error" || r.Code != "authorization" {
				t.Fatal("excluded resource admitted", r)
			}
			want := []string{"10.254.0.2:53"}
			if tc.allowed {
				want = append(want, tc.address+":27017")
			}
			for _, target := range want {
				select {
				case got := <-targets:
					if got != target {
						t.Fatalf("target %s, want %s", got, target)
					}
				case <-time.After(time.Second):
					t.Fatal("missing SOCKS target", target)
				}
			}
			select {
			case extra := <-targets:
				t.Fatal("unexpected destination", extra)
			default:
			}
		})
	}
}

// Directly invoke the TCP handler to check its independent authorization gate,
// even when a caller has not passed through carrier packet validation.
type subnetTCPConn struct {
	net.Conn
	destination net.Addr
}

func (c *subnetTCPConn) LocalAddr() net.Addr         { return c.destination }
func (*subnetTCPConn) ID() stack.TransportEndpointID { return stack.TransportEndpointID{} }

func TestTCPHandlerChecksSelectedSubnetBeforeSOCKS(t *testing.T) {
	for _, tc := range []struct {
		address string
		allowed bool
	}{
		{"10.254.1.3", true}, {"10.254.32.3", true},
		{"10.254.0.2", false}, {"10.254.0.9", false}, {"10.254.33.3", false},
		{"10.253.1.3", false}, {"::ffff:10.254.1.3", false}, {"fd00::3", false},
	} {
		t.Run(tc.address, func(t *testing.T) {
			proxy, targets := subnetProbeProxy(t, "10.254.1.3")
			ctx, cancel := context.WithCancel(context.Background())
			ranges, err := prefixes(subnetRanges(254, 32))
			if err != nil {
				t.Fatal(err)
			}
			u := &unit{ctx: ctx, cancel: cancel, proxy: proxy, cidrs: ranges, connections: make(map[net.Conn]struct{}), slots: make(chan struct{}, maxStreamsPerUnit)}
			t.Cleanup(u.close)
			client, peer := net.Pipe()
			defer func() { _ = peer.Close() }()
			// Use a numeric address verbatim so mapped IPv6 does not normalize to IPv4.
			connection := &subnetTCPConn{Conn: client, destination: numericTestAddr(netip.AddrPortFrom(netip.MustParseAddr(tc.address), 27017).String())}
			u.HandleTCP(connection)
			_ = peer.Close()
			done := make(chan struct{})
			go func() { u.workers.Wait(); close(done) }()
			select {
			case <-done:
			case <-time.After(4 * time.Second):
				t.Fatal("TCP worker did not stop")
			}
			if tc.allowed {
				select {
				case target := <-targets:
					if target != tc.address+":27017" {
						t.Fatal("wrong SOCKS destination", target)
					}
				default:
					t.Fatal("selected resource did not reach SOCKS")
				}
			} else {
				select {
				case target := <-targets:
					t.Fatal("excluded address reached SOCKS", target)
				default:
				}
			}
		})
	}
}

type numericTestAddr string

func (a numericTestAddr) String() string { return string(a) }
func (numericTestAddr) Network() string  { return "tcp" }
