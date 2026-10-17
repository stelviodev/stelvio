package dnsrelay

import (
	"context"
	"encoding/binary"
	"errors"
	"io"
	"net"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/miekg/dns"
)

func config(proxy string, revision uint64) Config {
	return Config{Revision: revision, Proxy: proxy, Resolver: netip.MustParseAddr("10.254.0.2"), CIDRs: []netip.Prefix{netip.MustParsePrefix("10.254.0.0/16")}, Domains: []string{"private.test"}, Hostnames: []string{"member.docdb.test"}}
}

// A real SOCKS server verifies numeric addressing and DNS-over-TCP framing.
func proxy(t *testing.T, answer func(*dns.Msg) *dns.Msg) string {
	t.Helper()
	l, err := net.Listen("tcp4", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	var wg sync.WaitGroup
	finished := make(chan struct{})
	go func() {
		defer close(finished)
		for {
			c, e := l.Accept()
			if e != nil {
				return
			}
			wg.Add(1)
			go func() {
				defer wg.Done()
				defer c.Close()
				_ = c.SetDeadline(time.Now().Add(5 * time.Second))
				greeting := make([]byte, 3)
				if _, e := io.ReadFull(c, greeting); e != nil {
					return
				}
				if string(greeting) != string([]byte{5, 1, 0}) {
					t.Error("unexpected SOCKS authentication")
					return
				}
				_, _ = c.Write([]byte{5, 0})
				req := make([]byte, 10)
				if _, e := io.ReadFull(c, req); e != nil {
					return
				}
				if req[0] != 5 || req[1] != 1 || req[3] != 1 || req[4] != 10 {
					t.Error("DNS used hostname or nonprivate SOCKS destination")
					return
				}
				_, _ = c.Write([]byte{5, 0, 0, 1, 127, 0, 0, 1, 0, 0})
				if binary.BigEndian.Uint16(req[8:]) != 53 {
					return
				}
				dc := &dns.Conn{Conn: c}
				q, e := dc.ReadMsg()
				if e != nil {
					return
				}
				a := answer(q)
				if a != nil {
					_ = dc.WriteMsg(a)
				}
			}()
		}
	}()
	t.Cleanup(func() { _ = l.Close(); <-finished; wg.Wait() })
	return l.Addr().String()
}
func newRelay(t *testing.T, c Config) *Relay {
	t.Helper()
	r, e := New(c)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(r.Close)
	return r
}
func query(t *testing.T, r *Relay, name string, udp bool) *dns.Msg {
	t.Helper()
	q := new(dns.Msg)
	q.SetQuestion(dns.Fqdn(name), dns.TypeA)
	wire, _ := q.Pack()
	wire, e := r.Answer(wire, udp)
	if e != nil {
		t.Fatal(e)
	}
	a := new(dns.Msg)
	if a.Unpack(wire) != nil {
		t.Fatal("invalid answer")
	}
	return a
}
func TestWireRecordsAndListenerFallback(t *testing.T) {
	p := proxy(t, func(q *dns.Msg) *dns.Msg {
		a := new(dns.Msg)
		a.SetReply(q)
		a.Answer = []dns.RR{
			&dns.CNAME{Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeCNAME, Class: 1, Ttl: 30}, Target: "member.private.test."},
			&dns.A{Hdr: dns.RR_Header{Name: "member.private.test.", Rrtype: dns.TypeA, Class: 1, Ttl: 12}, A: net.IPv4(10, 254, 1, 3)},
			&dns.RFC3597{Hdr: dns.RR_Header{Name: "member.private.test.", Rrtype: 65280, Class: 1, Ttl: 9}, Rdata: strings.Repeat("ab", 900)},
		}
		return a
	})
	r := newRelay(t, config(p, 1))
	q := new(dns.Msg)
	q.SetQuestion("db.private.test.", dns.TypeA)
	client := &dns.Client{Net: "udp", Timeout: time.Second}
	a, _, err := client.Exchange(q, r.udp.LocalAddr().String())
	if err != nil {
		t.Fatal(err)
	}
	if !a.Truncated {
		t.Fatal("oversized UDP answer was not truncated")
	}
	client.Net = "tcp"
	a, _, err = client.Exchange(q, r.tcp.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	if a.Truncated || len(a.Answer) != 3 || a.Answer[1].Header().Ttl != 12 {
		t.Fatalf("records/TTLs lost: %v", a)
	}
	q.SetEdns0(MaxUDP, false)
	client.Net = "udp"
	a, _, err = client.Exchange(q, r.udp.LocalAddr().String())
	if err != nil || a.Truncated || len(a.Answer) != 3 {
		t.Fatalf("EDNS: %v %v", a, err)
	}
}
func TestOwnershipFailureAndValidation(t *testing.T) {
	for _, fault := range []string{"nxdomain", "wrong-id", "wrong-question", "tc", "not-response", "rcode", "outside"} {
		t.Run(fault, func(t *testing.T) {
			p := proxy(t, func(q *dns.Msg) *dns.Msg {
				a := new(dns.Msg)
				a.SetReply(q)
				switch fault {
				case "nxdomain":
					a.Rcode = dns.RcodeNameError
				case "wrong-id":
					a.Id++
				case "wrong-question":
					a.Question[0].Name = "other.test."
				case "tc":
					a.Truncated = true
				case "not-response":
					a.Response = false
				case "rcode":
					a.Rcode = dns.RcodeServerFailure
				case "outside":
					a.Answer = []dns.RR{&dns.A{Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeA, Class: 1}, A: net.IPv4(192, 0, 2, 1)}}
				}
				return a
			})
			r := newRelay(t, config(p, 1))
			for _, name := range []string{"public.test", "private.test.public.test", "child.member.docdb.test"} {
				if query(t, r, name, true).Rcode != dns.RcodeRefused {
					t.Fatal("unowned query accepted")
				}
			}
			a := query(t, r, "db.private.test", false)
			want := dns.RcodeServerFailure
			if fault == "nxdomain" {
				want = dns.RcodeNameError
			}
			if fault == "outside" {
				want = dns.RcodeSuccess
			}
			if a.Rcode != want {
				t.Fatalf("rcode %d want %d", a.Rcode, want)
			}
			if fault == "outside" && r.Probe(1, "db.private.test", 27017, false) != ErrAuthorization {
				t.Fatal("foreign resource admitted")
			}
			if err := r.Reject(1); err != nil {
				t.Fatal(err)
			}
			if query(t, r, "member.docdb.test", false).Rcode != dns.RcodeServerFailure {
				t.Fatal("outage lost ownership")
			}
		})
	}
}
func TestGenerationsObservationsAndTwoVPCs(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	p := proxy(t, func(q *dns.Msg) *dns.Msg { close(started); <-release; a := new(dns.Msg); a.SetReply(q); return a })
	r := newRelay(t, config(p, 1))
	port := r.Port()
	if _, e := r.Observe("arm", "stlv.private.test", 1); e != nil {
		t.Fatal(e)
	}
	result := make(chan *dns.Msg, 1)
	go func() { result <- query(t, r, "stlv.private.test", false) }()
	<-started
	if seen, e := r.Observe("check", "stlv.private.test", 1); e != nil || !seen {
		t.Fatal("query not observed")
	}
	p2 := proxy(t, func(q *dns.Msg) *dns.Msg { a := new(dns.Msg); a.SetReply(q); a.Rcode = dns.RcodeNameError; return a })
	if e := r.Reject(1); e != nil {
		t.Fatal(e)
	}
	if e := r.Activate(config(p2, 2)); e != nil {
		t.Fatal(e)
	}
	close(release)
	if (<-result).Rcode != dns.RcodeServerFailure {
		t.Fatal("stale answer escaped")
	}
	if r.Reject(1) != ErrStale {
		t.Fatal("stale rejection accepted")
	}
	if _, e := r.Observe("check", "stlv.private.test", 1); e != ErrStale {
		t.Fatal("stale observation accepted")
	}
	if r.Port() != port || query(t, r, "db.private.test", false).Rcode != dns.RcodeNameError {
		t.Fatal("reconnect replaced listener or did not recover")
	}
	c := config(p2, 1)
	c.Domains = []string{"second.test"}
	c.Hostnames = nil
	c.Resolver = netip.MustParseAddr("10.253.0.2")
	c.CIDRs = []netip.Prefix{netip.MustParsePrefix("10.253.0.0/16")}
	second := newRelay(t, c)
	if query(t, second, "db.private.test", true).Rcode != dns.RcodeRefused {
		t.Fatal("cross-VPC DNS")
	}
	_ = r.Reject(2)
	if query(t, second, "db.second.test", false).Rcode != dns.RcodeNameError {
		t.Fatal("first VPC failure affected second")
	}
}
func TestBoundsAndConcurrentShutdown(t *testing.T) {
	p := proxy(t, func(q *dns.Msg) *dns.Msg { a := new(dns.Msg); a.SetReply(q); return a })
	r := newRelay(t, config(p, 1))
	for i := 0; i < MaxRequests; i++ {
		r.slots <- struct{}{}
	}
	if query(t, r, "db.private.test", false).Rcode != dns.RcodeServerFailure {
		t.Fatal("overload admitted")
	}
	for i := 0; i < MaxRequests; i++ {
		<-r.slots
	}
	c, e := net.Dial("tcp4", r.tcp.Addr().String())
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	_, _ = c.Write([]byte{255}) // Slow partial DNS frame.
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); r.Close() }()
	}
	wg.Wait()
	if _, e := r.Answer([]byte{0, 1}, true); e == nil {
		t.Fatal("malformed request accepted")
	}
}
func FuzzAnswer(f *testing.F) {
	// Independent root SOA wire vector; reject the view so fuzzing never dials.
	f.Add([]byte{0, 1, 1, 0, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0, 6, 0, 1})
	r, e := New(config("127.0.0.1:65534", 1))
	if e != nil {
		f.Fatal(e)
	}
	defer r.Close()
	_ = r.Reject(1)
	f.Fuzz(func(t *testing.T, wire []byte) {
		answer, e := r.Answer(wire, true)
		if e == nil {
			a := new(dns.Msg)
			if len(answer) > MaxUDP || a.Unpack(answer) != nil || !a.Response {
				t.Fatal("invalid bounded DNS reply")
			}
		}
	})
}

func TestOverloadUsesIdenticalPolicy(t *testing.T) {
	r := newRelay(t, config("127.0.0.1:65534", 1))
	for _, name := range []string{"db.private.test.", "outside.test."} {
		q := new(dns.Msg)
		q.SetQuestion(name, dns.TypeA)
		wire, _ := q.Pack()
		answer, err := r.answer(wire, true, false)
		if err != nil {
			t.Fatal(err)
		}
		a := new(dns.Msg)
		_ = a.Unpack(answer)
		expected := dns.RcodeServerFailure
		if name == "outside.test." {
			expected = dns.RcodeRefused
		}
		if a.Rcode != expected {
			t.Fatal("overload ownership policy", a)
		}
		q.Response = true
		wire, _ = q.Pack()
		if _, err = r.answer(wire, true, false); err == nil {
			t.Fatal("answered a response packet")
		}
	}
}

func TestDirectProbeRequiresMatchingPayloadAndResourceConnection(t *testing.T) {
	for _, fault := range []string{"success", "missing", "wrong-id", "wrong-question", "tc", "rcode", "foreign"} {
		t.Run(fault, func(t *testing.T) {
			p := proxy(t, func(q *dns.Msg) *dns.Msg { a := new(dns.Msg); a.SetReply(q); return a })
			r := newRelay(t, config(p, 1))
			// SOCKS readiness succeeds independently of the direct-host boundary below.
			if err := r.Probe(1, "", 0, false); err != nil {
				t.Fatal(err)
			}
			var workers sync.WaitGroup
			defer workers.Wait()
			connections := 0
			r.current.dialContext = func(ctx context.Context, network, target string) (net.Conn, error) {
				if network != "tcp4" {
					t.Fatal(network)
				}
				c, peer := net.Pipe()
				workers.Add(1)
				connections++
				if target == "10.254.0.2:53" {
					go func() {
						defer workers.Done()
						defer peer.Close()
						_ = peer.SetDeadline(time.Now().Add(time.Second))
						dc := &dns.Conn{Conn: peer}
						q, e := dc.ReadMsg()
						if e != nil {
							return
						}
						a := new(dns.Msg)
						a.SetReply(q)
						switch fault {
						case "missing":
							return
						case "wrong-id":
							a.Id++
						case "wrong-question":
							a.Question[0].Name = "elsewhere.test."
						case "tc":
							a.Truncated = true
						case "rcode":
							a.Rcode = dns.RcodeServerFailure
						}
						if q.Question[0].Qtype == dns.TypeA {
							ip := net.IPv4(10, 254, 1, 3)
							if fault == "foreign" {
								ip = net.IPv4(192, 0, 2, 1)
							}
							a.Answer = []dns.RR{&dns.A{Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeA, Class: 1}, A: ip}}
						}
						_ = dc.WriteMsg(a)
					}()
				} else if target == "10.254.1.3:27017" {
					go func() { defer workers.Done(); _ = peer.Close() }()
				} else {
					t.Error("direct probe used SOCKS or a hostname", target)
					workers.Done()
					_ = peer.Close()
				}
				return c, nil
			}
			err := r.Probe(1, "db.private.test", 27017, true)
			if fault == "success" {
				if err != nil || connections != 2 {
					t.Fatal("direct resource path not verified", err, connections)
				}
			} else if err == nil {
				t.Fatal("SOCKS success masked failed direct payload", fault)
			}
			if fault == "foreign" && err != ErrAuthorization {
				t.Fatal(err)
			}
		})
	}
}

func TestTCPConnectionQueryLimitAndDeadline(t *testing.T) {
	r := newRelay(t, config("127.0.0.1:65534", 1))
	_ = r.Reject(1)
	c, e := net.Dial("tcp4", r.tcp.Addr().String())
	if e != nil {
		t.Fatal(e)
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(5 * time.Second))
	dc := &dns.Conn{Conn: c}
	q := new(dns.Msg)
	q.SetQuestion("db.private.test.", dns.TypeA)
	for i := 0; i < 8; i++ {
		if dc.WriteMsg(q) != nil {
			t.Fatal("early TCP close")
		}
		if _, e = dc.ReadMsg(); e != nil {
			t.Fatal(e)
		}
	}
	if e = dc.WriteMsg(q); e == nil {
		if _, e = dc.ReadMsg(); e == nil {
			t.Fatal("accepted ninth query")
		}
	}
	slow, e := net.Dial("tcp4", r.tcp.Addr().String())
	if e != nil {
		t.Fatal(e)
	}
	defer slow.Close()
	_ = slow.SetDeadline(time.Now().Add(5 * time.Second))
	_, _ = slow.Write([]byte{1})
	if _, e = slow.Read(make([]byte, 1)); e == nil {
		t.Fatal("slow partial client survived deadline")
	}
	var timeout net.Error
	if errors.As(e, &timeout) && timeout.Timeout() {
		t.Fatal("client timeout masked missing server deadline", e)
	}
	address := r.tcp.Addr().String()
	r.Close()
	if c, e := net.DialTimeout("tcp4", address, time.Second); e == nil {
		_ = c.Close()
		t.Fatal("closed listener still reachable")
	}
}

func TestWireSaturationKeepsOwnershipAndResponsePolicy(t *testing.T) {
	started := make(chan struct{}, MaxRequests)
	release := make(chan struct{})
	defer close(release)
	p := proxy(t, func(q *dns.Msg) *dns.Msg {
		started <- struct{}{}
		<-release
		a := new(dns.Msg)
		a.SetReply(q)
		return a
	})
	r := newRelay(t, config(p, 1))
	client, e := net.DialUDP("udp4", nil, r.udp.LocalAddr().(*net.UDPAddr))
	if e != nil {
		t.Fatal(e)
	}
	defer client.Close()
	_ = client.SetDeadline(time.Now().Add(3 * time.Second))
	q := new(dns.Msg)
	q.SetQuestion("db.private.test.", dns.TypeA)
	wire, _ := q.Pack()
	for i := 0; i < MaxRequests; i++ {
		if _, e = client.Write(wire); e != nil {
			t.Fatal(e)
		}
	}
	for i := 0; i < MaxRequests; i++ {
		select {
		case <-started:
		case <-time.After(2 * time.Second):
			t.Fatal("request did not reach upstream")
		}
	}
	for _, test := range []struct {
		name string
		code int
	}{{"db.private.test.", dns.RcodeServerFailure}, {"outside.test.", dns.RcodeRefused}} {
		q.SetQuestion(test.name, dns.TypeA)
		wire, _ = q.Pack()
		if _, e = client.Write(wire); e != nil {
			t.Fatal(e)
		}
		b := make([]byte, MaxUDP)
		n, e := client.Read(b)
		if e != nil {
			t.Fatal(e)
		}
		a := new(dns.Msg)
		if a.Unpack(b[:n]) != nil || a.Rcode != test.code {
			t.Fatal("wire overload policy", a)
		}
	}
	q.Response = true
	wire, _ = q.Pack()
	_, _ = client.Write(wire)
	_ = client.SetReadDeadline(time.Now().Add(50 * time.Millisecond))
	if _, e = client.Read(make([]byte, MaxUDP)); e == nil {
		t.Fatal("response packet answered during overload")
	}
}
