package dnsrelay

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/netip"
	"testing"

	"github.com/miekg/dns"
	"github.com/stelviodev/traforo/internal/protocol"
)

func TestResolverIsIndependentOfResourceRanges(t *testing.T) {
	for _, address := range []string{"10.254.0.2", "172.16.0.2", "192.168.0.2"} {
		t.Run(address, func(t *testing.T) {
			c := config("127.0.0.1:1080", 1)
			c.Resolver = netip.MustParseAddr(address)
			c.CIDRs = []netip.Prefix{netip.MustParsePrefix("10.254.16.0/20")}
			if err := c.Validate(); err != nil {
				t.Fatal(err)
			}
			if c.ownsAddress(c.Resolver) {
				t.Fatal("resolver gained resource authorization")
			}
		})
	}
	for _, address := range []string{"", "8.8.8.8", "192.0.2.1", "127.0.0.1", "169.254.169.253", "100.64.0.2", "0.0.0.0", "224.0.0.1", "::1", "fd00::2", "::ffff:10.254.0.2"} {
		t.Run("reject-"+address, func(t *testing.T) {
			c := config("127.0.0.1:1080", 1)
			c.Resolver, _ = netip.ParseAddr(address)
			if err := c.Validate(); err == nil {
				t.Fatal("invalid resolver accepted")
			}
		})
	}
}

func TestSubnetResourceProbeAuthorization(t *testing.T) {
	for _, direct := range []bool{false, true} {
		for _, address := range []string{"10.254.0.2", "10.254.0.9", "10.254.15.255", "10.254.32.0", "10.254.48.1", "10.253.16.1", "192.0.2.1"} {
			t.Run(fmt.Sprintf("%s/direct=%t", address, direct), func(t *testing.T) {
				p := proxyAt(t, "10.254.0.2", func(q *dns.Msg) *dns.Msg {
					a := new(dns.Msg)
					a.SetReply(q)
					// A mixture of valid and invalid answers must fail before connecting.
					for _, ip := range []string{"10.254.16.3", address} {
						a.Answer = append(a.Answer, &dns.A{Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeA, Class: 1}, A: net.ParseIP(ip).To4()})
					}
					return a
				})
				c := config(p, 1)
				c.CIDRs = []netip.Prefix{netip.MustParsePrefix("10.254.16.0/20"), netip.MustParsePrefix("10.254.64.0/20")}
				r := newRelay(t, c)
				dials := 0
				r.current.dialContext = func(ctx context.Context, network, target string) (net.Conn, error) {
					dials++
					if target != p {
						t.Errorf("resource dial before authorization: %s", target)
						return nil, errors.New("unexpected host dial")
					}
					return (&net.Dialer{}).DialContext(ctx, network, target)
				}
				if err := r.Probe(1, "db.private.test", 27017, direct); err != ErrAuthorization {
					t.Fatalf("foreign answer: %v", err)
				}
				if dials != 1 {
					t.Fatalf("got %d dials; only DNS may connect", dials)
				}
				// Port 53 on the resolver must not bypass the resource dial gate.
				if _, err := dial(r.current, netip.AddrPortFrom(netip.MustParseAddr(address), 53), direct); err != ErrAuthorization {
					t.Fatalf("resource dial bypass: %v", err)
				}
				if dials != 1 {
					t.Fatal("rejected resource reached IO")
				}
			})
		}
	}
}

func TestResourceProbeAcrossMaximumSubnetSet(t *testing.T) {
	for _, count := range []int{20, protocol.MaxRanges} {
		t.Run(fmt.Sprint(count), func(t *testing.T) {
			for _, index := range []int{0, count - 1} {
				address := fmt.Sprintf("10.254.%d.3", index+1)
				p := proxyAt(t, "10.254.0.2", func(q *dns.Msg) *dns.Msg {
					a := new(dns.Msg)
					a.SetReply(q)
					a.Answer = []dns.RR{&dns.A{Hdr: dns.RR_Header{Name: q.Question[0].Name, Rrtype: dns.TypeA, Class: 1, Ttl: 17}, A: net.ParseIP(address).To4()}}
					return a
				})
				c := config(p, 1)
				c.CIDRs = nil
				for i := 0; i < count; i++ {
					c.CIDRs = append(c.CIDRs, netip.MustParsePrefix(fmt.Sprintf("10.254.%d.0/24", i+1)))
				}
				r := newRelay(t, c)
				if err := r.Probe(1, "db.private.test", 27017, false); err != nil {
					t.Fatalf("selected range %d/%d: %v", index, count, err)
				}
				a := query(t, r, "db.private.test", false)
				if a.Rcode != dns.RcodeSuccess || len(a.Answer) != 1 || a.Answer[0].Header().Ttl != 17 {
					t.Fatal("relay altered the DNS answer")
				}
			}
		})
	}
}
