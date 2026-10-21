package forwarder

import (
	"context"
	"encoding/binary"
	"fmt"
	"net"
	"net/netip"
	"sync"
	"testing"
	"time"

	"github.com/stelviodev/traforo/internal/protocol"
)

func subnetRanges(octet byte, count int) []string {
	values := make([]string, count)
	for i := range values {
		values[i] = fmt.Sprintf("10.%d.%d.0/24", octet, i+1)
	}
	return values
}

func TestSelectedSubnetPrefixValidation(t *testing.T) {
	for _, count := range []int{0, 1, 8, 20, 32, 33} {
		t.Run(fmt.Sprint(count), func(t *testing.T) {
			values := subnetRanges(254, count)
			got, err := prefixes(values)
			if count == 0 || count > protocol.MaxRanges {
				if err == nil {
					t.Fatal("invalid range count accepted")
				}
				return
			}
			if err != nil || len(got) != count {
				t.Fatalf("ranges: %v, %v", got, err)
			}
			for i, p := range got {
				if p.String() != values[i] {
					t.Fatal("ranges changed or merged")
				}
			}
			values[0] = "10.254.0.0/16"
			if got[0].String() != "10.254.1.0/24" {
				t.Fatal("caller changed retained ranges")
			}
		})
	}
	for _, values := range [][]string{
		{"10.254.1.0/24", "10.254.1.0/24"}, {"10.254.1.0/24", "10.254.1.0/25"},
		{"10.254.1.1/24"}, {"10.254.1.0/024"}, {"10.254.0.0/15"}, {"10.254.1.0/29"},
		{"8.8.8.0/24"}, {"127.0.0.0/16"}, {"169.254.0.0/16"}, {"100.64.0.0/16"}, {"fd00::/16"}, {"::ffff:10.254.1.0/120"},
	} {
		t.Run(fmt.Sprint(values), func(t *testing.T) {
			if _, err := prefixes(values); err == nil {
				t.Fatal("invalid subnet set accepted")
			}
		})
	}
	for _, value := range []string{"10.254.0.0/16", "172.16.1.0/24", "192.168.1.0/28"} {
		if _, err := prefixes([]string{value}); err != nil {
			t.Fatalf("valid explicit range %s: %v", value, err)
		}
	}
}

// This fixture checks the range gate without a TCP stack. Real checksums, stream
// payloads and half-close behavior are exercised by the large-stream regression.
func subnetPacket(remote string, returning bool) []byte {
	packet := make([]byte, 40)
	packet[0] = 0x45
	packet[9] = 6
	packet[32] = 0x50
	binary.BigEndian.PutUint16(packet[2:], 40)
	local := netip.MustParseAddr("192.0.2.1").As4()
	address := netip.MustParseAddr(remote).As4()
	if returning {
		copy(packet[12:16], address[:])
		copy(packet[16:20], local[:])
	} else {
		copy(packet[12:16], local[:])
		copy(packet[16:20], address[:])
	}
	return packet
}

type subnetCarrier struct{ writes [][]byte }

func (*subnetCarrier) SetWriteDeadline(time.Time) error { return nil }
func (c *subnetCarrier) WriteMsgUnix(p, oob []byte, peer *net.UnixAddr) (int, int, error) {
	c.writes = append(c.writes, append([]byte(nil), p...))
	return len(p), 0, nil
}

func TestSubnetPacketAndReturnAuthorization(t *testing.T) {
	ranges, err := prefixes([]string{"10.254.16.0/20", "10.254.64.0/20"})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	carrier := new(subnetCarrier)
	u := &unit{id: 3, generation: 7, cidrs: ranges, ctx: ctx, carrier: carrier, writer: new(sync.Mutex)}
	for _, tc := range []struct {
		address string
		allowed bool
	}{
		{"10.254.16.0", true}, {"10.254.31.255", true}, {"10.254.64.0", true}, {"10.254.79.255", true},
		{"10.254.0.2", false}, {"10.254.0.9", false}, {"10.254.15.255", false}, {"10.254.32.0", false}, {"10.254.48.1", false}, {"10.254.80.0", false}, {"10.253.16.1", false}, {"8.8.8.8", false},
	} {
		t.Run(tc.address, func(t *testing.T) {
			for _, returning := range []bool{false, true} {
				if u.accepts(subnetPacket(tc.address, returning), returning) != tc.allowed {
					t.Fatalf("incorrect range decision returning=%t", returning)
				}
			}
			packet := subnetPacket(tc.address, true)
			before := len(carrier.writes)
			n, err := u.Write(packet)
			if !tc.allowed {
				if err == nil || n != 0 || len(carrier.writes) != before {
					t.Fatal("excluded source reached return carrier")
				}
				return
			}
			if err != nil || n != len(packet) || len(carrier.writes) != before+1 {
				t.Fatalf("valid return rejected: %d %v", n, err)
			}
			frame := carrier.writes[before]
			if binary.BigEndian.Uint32(frame) != 3 || binary.BigEndian.Uint64(frame[4:]) != 7 || binary.BigEndian.Uint32(frame[12:]) != protocol.IPv4 {
				t.Fatal("return identity changed")
			}
		})
	}
}
