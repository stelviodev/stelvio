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
	"sync/atomic"
	"testing"
	"time"

	"github.com/miekg/dns"
	"github.com/stelviodev/traforo/internal/protocol"
)

func config(proxy string, revision uint64) Config {
	return Config{Revision: revision, Proxy: proxy, Resolver: netip.MustParseAddr("10.254.0.2"), CIDRs: []netip.Prefix{netip.MustParsePrefix("10.254.1.0/24")}, Domains: []string{"private.test"}, Hostnames: []string{"member.docdb.test"}}
}

func rawQuery(name string, qtype uint16) []byte {
	wire := make([]byte, 12)
	binary.BigEndian.PutUint16(wire[0:2], 0x1234)
	binary.BigEndian.PutUint16(wire[2:4], 0x0100)
	binary.BigEndian.PutUint16(wire[4:6], 1)
	for _, label := range strings.Split(name, ".") {
		wire = append(wire, byte(len(label)))
		wire = append(wire, label...)
	}
	wire = append(wire, 0, byte(qtype>>8), byte(qtype), 0, 1)
	return wire
}

func offlineRelay() *Relay {
	c := config("127.0.0.1:65534", 1)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	return &Relay{
		current:      &view{config: c, ctx: ctx, cancel: cancel},
		observations: make(map[string]observation),
		slots:        make(chan struct{}, MaxRequests),
	}
}

func TestConfigValidation(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*Config)
	}{
		{"nonprivate resolver", func(c *Config) { c.Resolver = netip.MustParseAddr("192.0.2.1") }},
		{"non-loopback proxy", func(c *Config) { c.Proxy = "10.254.0.3:1080" }},
		{"noncanonical domain", func(c *Config) { c.Domains = []string{"Private.test"} }},
		{"IP hostname", func(c *Config) { c.Hostnames = []string{"10.254.0.3"} }},
		{"duplicate domain", func(c *Config) { c.Domains = []string{"private.test", "private.test"} }},
		{"invalid CIDR", func(c *Config) { c.CIDRs = []netip.Prefix{netip.MustParsePrefix("10.0.0.1/24")} }},
		{"overlapping CIDRs", func(c *Config) { c.CIDRs = append(c.CIDRs, netip.MustParsePrefix("10.254.1.0/24")) }},
		{"too many CIDRs", func(c *Config) {
			c.CIDRs = nil
			for i := 0; i <= protocol.MaxRanges; i++ {
				c.CIDRs = append(c.CIDRs, netip.MustParsePrefix(fmt.Sprintf("10.%d.0.0/16", i)))
			}
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			c := config("127.0.0.1:1080", 1)
			tc.change(&c)
			if err := c.Validate(); err == nil {
				t.Fatal("invalid config accepted")
			}
		})
	}
}

func TestAnswerRejectsOversizedUDPInput(t *testing.T) {
	if _, err := (&Relay{}).Answer(make([]byte, MaxUDP+1), true); err == nil {
		t.Fatal("oversized UDP message accepted")
	}
}

func TestAnswerAcceptsMaximumTCPMessage(t *testing.T) {
	wire := rawQuery("db.private.test", dns.TypeA)
	wire[10], wire[11] = 0, 1 // One additional OPT record.
	paddingLength := 65535 - len(wire) - 11 - 4
	wire = append(wire, 0, byte(dns.TypeOPT>>8), byte(dns.TypeOPT), 0x10, 0, 0, 0, 0, 0, byte((paddingLength+4)>>8), byte(paddingLength+4))
	wire = append(wire, 0, 12, byte(paddingLength>>8), byte(paddingLength))
	wire = append(wire, make([]byte, paddingLength)...)
	if len(wire) != 65535 {
		t.Fatalf("test packet length=%d, want 65535", len(wire))
	}
	answer, err := offlineRelay().Answer(wire, false)
	if err != nil {
		t.Fatalf("maximum-sized valid DNS message rejected: %v", err)
	}
	a := new(dns.Msg)
	if err = a.Unpack(answer); err != nil || !a.Response || a.Rcode != dns.RcodeServerFailure {
		t.Fatalf("unexpected bounded reply: %v, %v", a, err)
	}
}

func TestServiceDiscoveryQuestionUsesOwnedSuffix(t *testing.T) {
	p := proxy(t, func(q *dns.Msg) *dns.Msg {
		a := new(dns.Msg)
		a.SetReply(q)
		a.Answer = []dns.RR{&dns.SRV{Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeSRV, Class: dns.ClassINET, Ttl: 21}, Port: 27017, Target: "member.private.test."}}
		return a
	})
	r := newRelay(t, config(p, 1))
	answer, err := r.Answer(rawQuery("_mongodb._tcp.private.test", dns.TypeSRV), true)
	if err != nil {
		t.Fatal(err)
	}
	a := new(dns.Msg)
	if err := a.Unpack(answer); err != nil {
		t.Fatal(err)
	}
	if a.Rcode != dns.RcodeSuccess || len(a.Answer) != 1 || a.Answer[0].Header().Ttl != 21 {
		t.Fatalf("service-discovery answer was not relayed: %v", a)
	}
}

func TestNoDNSAnswerCache(t *testing.T) {
	var calls atomic.Int32
	p := proxy(t, func(q *dns.Msg) *dns.Msg {
		call := calls.Add(1)
		a := new(dns.Msg)
		a.SetReply(q)
		a.Answer = []dns.RR{&dns.A{Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeA, Class: dns.ClassINET, Ttl: uint32(call)}, A: net.IPv4(10, 254, 1, byte(call))}}
		return a
	})
	r := newRelay(t, config(p, 1))
	for i := 1; i <= 2; i++ {
		a := query(t, r, "db.private.test", false)
		if calls.Load() != int32(i) || len(a.Answer) != 1 || a.Answer[0].Header().Ttl != uint32(i) {
			t.Fatalf("query %d was served from cache or changed: calls=%d answer=%v", i, calls.Load(), a)
		}
	}
}

// A real SOCKS server verifies numeric addressing and DNS-over-TCP framing.
func proxy(t *testing.T, answer func(*dns.Msg) *dns.Msg) string {
	t.Helper()
	return proxyAt(t, "", answer)
}

func proxyAt(t *testing.T, resolver string, answer func(*dns.Msg) *dns.Msg) string {
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
				defer func() { _ = c.Close() }()
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
				if resolver != "" && net.IP(req[4:8]).String() != resolver {
					t.Errorf("DNS SOCKS target = %s, want %s", net.IP(req[4:8]), resolver)
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
	type answerResult struct {
		answer *dns.Msg
		err    error
	}
	result := make(chan answerResult, 1)
	go func() {
		q := new(dns.Msg)
		q.SetQuestion("stlv.private.test.", dns.TypeA)
		wire, err := q.Pack()
		if err != nil {
			result <- answerResult{err: err}
			return
		}
		wire, err = r.Answer(wire, false)
		if err != nil {
			result <- answerResult{err: err}
			return
		}
		a := new(dns.Msg)
		if err = a.Unpack(wire); err != nil {
			result <- answerResult{err: err}
			return
		}
		result <- answerResult{answer: a}
	}()
	<-started
	if seen, e := r.Observe("check", "stlv.private.test", 1); e != nil || !seen {
		t.Fatal("query not observed")
	}
	p2 := proxy(t, func(q *dns.Msg) *dns.Msg { a := new(dns.Msg); a.SetReply(q); a.Rcode = dns.RcodeNameError; return a })
	if e := r.Reject(1); e != nil {
		t.Fatal(e)
	}
	if _, e := r.Observe("check", "stlv.private.test", 1); e != ErrStale {
		t.Fatal("observation remained usable after transport loss")
	}
	if e := r.Activate(config(p2, 2)); e != nil {
		t.Fatal(e)
	}
	close(release)
	got := <-result
	if got.err != nil || got.answer == nil || got.answer.Rcode != dns.RcodeServerFailure {
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
	c.CIDRs = []netip.Prefix{netip.MustParsePrefix("10.253.1.0/24")}
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
	defer func() { _ = c.Close() }()
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
	r := offlineRelay()
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

func TestTCPOverloadReturnsPolicyResponse(t *testing.T) {
	r := newRelay(t, config("127.0.0.1:65534", 1))
	for i := 0; i < cap(r.tcpSlots); i++ {
		r.tcpSlots <- struct{}{}
	}

	for _, tc := range []struct {
		name string
		want int
	}{
		{name: "owned", want: dns.RcodeServerFailure},
		{name: "unowned.test.", want: dns.RcodeRefused},
	} {
		name := tc.name
		if name == "owned" {
			name = "db.private.test."
		}
		t.Run(name, func(t *testing.T) {
			c, err := net.DialTimeout("tcp4", r.tcp.Addr().String(), Timeout)
			if err != nil {
				t.Fatal(err)
			}
			defer func() { _ = c.Close() }()
			_ = c.SetDeadline(time.Now().Add(Timeout))
			q := new(dns.Msg)
			q.SetQuestion(name, dns.TypeA)
			if err := (&dns.Conn{Conn: c}).WriteMsg(q); err != nil {
				t.Fatal(err)
			}
			a, err := (&dns.Conn{Conn: c}).ReadMsg()
			if err != nil {
				t.Fatalf("overloaded TCP connection received no DNS response: %v", err)
			}
			if a.Rcode != tc.want {
				t.Fatalf("rcode=%s, want %s", dns.RcodeToString[a.Rcode], dns.RcodeToString[tc.want])
			}
		})
	}
}

func TestSlowTCPOverflowAllowsLegitimateConnectionAfterDeadline(t *testing.T) {
	p := proxy(t, func(q *dns.Msg) *dns.Msg {
		a := new(dns.Msg)
		a.SetReply(q)
		a.Answer = []dns.RR{&dns.A{
			Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeA, Class: dns.ClassINET, Ttl: 30},
			A:   net.IPv4(10, 254, 0, 9),
		}}
		return a
	})
	r := newRelay(t, config(p, 1))

	workers := make([]net.Conn, cap(r.tcpSlots))
	defer func() {
		for _, c := range workers {
			if c != nil {
				_ = c.Close()
			}
		}
	}()
	for i := range workers {
		c, err := net.DialTimeout("tcp4", r.tcp.Addr().String(), Timeout)
		if err != nil {
			t.Fatalf("open TCP worker %d: %v", i, err)
		}
		workers[i] = c
	}

	connectionCount := func() int {
		r.mu.Lock()
		defer r.mu.Unlock()
		return len(r.connections)
	}
	waitFor := func(label string, ready func() bool) {
		t.Helper()
		deadline := time.Now().Add(Timeout)
		for !ready() {
			if time.Now().After(deadline) {
				t.Fatalf("timed out waiting for %s", label)
			}
			time.Sleep(5 * time.Millisecond)
		}
	}
	waitFor("all TCP worker slots", func() bool {
		return len(r.tcpSlots) == cap(r.tcpSlots) && connectionCount() == cap(r.tcpSlots)
	})

	// Send only the first byte of the DNS-over-TCP length prefix. The inline
	// overload handler must wait for its bounded read deadline before accepting
	// the legitimate connection queued behind it.
	overflow, err := net.DialTimeout("tcp4", r.tcp.Addr().String(), Timeout)
	if err != nil {
		t.Fatalf("open overflow connection: %v", err)
	}
	defer func() { _ = overflow.Close() }()
	if _, err := overflow.Write([]byte{0}); err != nil {
		t.Fatalf("write partial DNS frame: %v", err)
	}
	waitFor("overflow handler", func() bool { return connectionCount() == cap(r.tcpSlots)+1 })

	legitimate, err := net.DialTimeout("tcp4", r.tcp.Addr().String(), Timeout)
	if err != nil {
		t.Fatalf("open legitimate connection: %v", err)
	}
	defer func() { _ = legitimate.Close() }()
	_ = legitimate.SetDeadline(time.Now().Add(2 * Timeout))
	q := new(dns.Msg)
	q.SetQuestion("db.private.test.", dns.TypeA)
	if err := (&dns.Conn{Conn: legitimate}).WriteMsg(q); err != nil {
		t.Fatalf("write legitimate DNS request: %v", err)
	}
	_ = legitimate.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
	if _, err := (&dns.Conn{Conn: legitimate}).ReadMsg(); err == nil {
		t.Fatal("accept loop served a queued connection before the overflow deadline")
	} else {
		var netErr net.Error
		if !errors.As(err, &netErr) || !netErr.Timeout() {
			t.Fatalf("queued connection read error = %v, want timeout", err)
		}
	}

	// Free a real worker while the accept loop is still blocked reading the
	// overflow request and the legitimate request is queued behind it.
	_ = workers[0].Close()
	workers[0] = nil
	waitFor("worker capacity", func() bool { return len(r.tcpSlots) < cap(r.tcpSlots) })

	_ = legitimate.SetDeadline(time.Now().Add(2 * Timeout))
	a, err := (&dns.Conn{Conn: legitimate}).ReadMsg()
	if err != nil {
		t.Fatalf("legitimate request was not served after overflow deadline: %v", err)
	}
	if a.Rcode != dns.RcodeSuccess || len(a.Answer) != 1 {
		t.Fatalf("legitimate DNS response = %v", a)
	}
	got, ok := a.Answer[0].(*dns.A)
	if !ok || !got.A.Equal(net.IPv4(10, 254, 0, 9)) {
		t.Fatalf("legitimate DNS answer = %v, want 10.254.0.9", a.Answer)
	}
}

func TestDirectProbeRequiresMatchingPayloadAndResourceConnection(t *testing.T) {
	for _, fault := range []string{"success", "missing", "wrong-id", "wrong-question", "tc", "rcode", "foreign", "resource-offline"} {
		t.Run(fault, func(t *testing.T) {
			p := proxy(t, func(q *dns.Msg) *dns.Msg {
				a := new(dns.Msg)
				a.SetReply(q)
				if q.Question[0].Qtype != dns.TypeA {
					return a
				}
				switch fault {
				case "missing":
					return nil
				case "wrong-id":
					a.Id++
				case "wrong-question":
					a.Question[0].Name = "elsewhere.test."
				case "tc":
					a.Truncated = true
				case "rcode":
					a.Rcode = dns.RcodeServerFailure
				}
				ip := net.IPv4(10, 254, 1, 3)
				if fault == "foreign" {
					ip = net.IPv4(192, 0, 2, 1)
				}
				a.Answer = []dns.RR{&dns.A{Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeA, Class: 1}, A: ip}}
				return a
			})
			c := config(p, 1)
			c.CIDRs = []netip.Prefix{netip.MustParsePrefix("10.254.1.0/24")}
			r := newRelay(t, c)
			// Resolver-only success cannot mask failure of the ordinary-host path.
			if err := r.Probe(1, "", 0, true); err != nil {
				t.Fatal(err)
			}
			connections := 0
			r.current.dialContext = func(ctx context.Context, network, target string) (net.Conn, error) {
				if network != "tcp4" {
					t.Fatal(network)
				}
				connections++
				switch target {
				case p:
					return (&net.Dialer{}).DialContext(ctx, network, target)
				case "10.254.1.3:27017":
					if fault == "resource-offline" {
						return nil, errors.New("test resource offline")
					}
					client, peer := net.Pipe()
					_ = peer.Close()
					return client, nil
				default:
					t.Errorf("unexpected host destination: %s", target)
					return nil, errors.New("unexpected destination")
				}
			}
			err := r.Probe(1, "db.private.test", 27017, true)
			if fault == "success" {
				if err != nil || connections != 2 {
					t.Fatal("direct resource path not verified", err, connections)
				}
			} else if err == nil {
				t.Fatal("SOCKS success masked failed direct resource probe", fault)
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
	defer func() { _ = c.Close() }()
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
	defer func() { _ = slow.Close() }()
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
	defer func() { _ = client.Close() }()
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
