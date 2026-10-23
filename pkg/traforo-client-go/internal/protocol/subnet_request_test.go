package protocol

import (
	"encoding/binary"
	"fmt"
	"net/netip"
	"strings"
	"testing"
)

// Handwrite wire fields independently of production decoding. Keep the shared
// golden vectors unchanged; this package does not have a production encoder.
func subnetRequest(ranges, domains []string) []byte {
	p := request(Configure)
	p[16], p[32] = 1, 2
	binary.BigEndian.PutUint64(p[48:], 3)
	body := []byte{0x12, 0x34, 0x56, 0x78}
	text := func(value string) {
		body = append(body, byte(len(value)))
		body = append(body, value...)
	}
	text("vpc-0123456789abcdef0")
	body = append(body, byte(len(ranges)))
	for _, value := range ranges {
		text(value)
	}
	body = append(body, byte(len(domains)))
	for _, value := range domains {
		text(value)
		body = append(body, 0x14, 0xb4) // Unprivileged loopback DNS port 5300.
	}
	binary.BigEndian.PutUint32(p[12:], uint32(len(body)))
	return append(p, body...)
}

func subnetRanges(count int) []string {
	ranges := make([]string, count)
	for i := range ranges {
		ranges[i] = fmt.Sprintf("10.254.%d.0/24", i)
	}
	return ranges
}

func TestSubnetRangeCounts(t *testing.T) {
	for _, count := range []int{0, 1, 8, 20, 32, 33, 255} {
		t.Run(fmt.Sprint(count), func(t *testing.T) {
			ranges := subnetRanges(count)
			r, err := Decode(subnetRequest(ranges, nil))
			if count == 0 || count > 32 {
				if err != ErrInvalid {
					t.Fatalf("accepted unsupported count %d: %v", count, err)
				}
				return
			}
			if err != nil || len(r.Ranges) != count {
				t.Fatalf("count %d: got %d ranges, error %v", count, len(r.Ranges), err)
			}
			for i, prefix := range r.Ranges {
				if prefix.String() != ranges[i] {
					t.Fatalf("range %d: got %s, want %s", i, prefix, ranges[i])
				}
			}
		})
	}
}

func TestTenZonePrivateAndIsolatedSubnetLayout(t *testing.T) {
	var ranges []string
	for zone := 0; zone < 10; zone++ {
		ranges = append(ranges, fmt.Sprintf("10.0.%d.0/22", 20+zone*4))
	}
	for zone := 0; zone < 10; zone++ {
		ranges = append(ranges, fmt.Sprintf("10.0.%d.0/24", 60+zone))
	}
	r, err := Decode(subnetRequest(ranges, nil))
	if err != nil || len(r.Ranges) != 20 {
		t.Fatalf("ten-zone layout: got %d ranges, error %v", len(r.Ranges), err)
	}
}

func TestMaximumSubnetRequestFitsBody(t *testing.T) {
	ranges := make([]string, 32)
	for i := range ranges {
		ranges[i] = fmt.Sprintf("192.168.%d.%d/28", 100+i/8, 128+(i%8)*16)
	}
	domains := make([]string, 64)
	for i := range domains {
		domains[i] = strings.Repeat("a", 63) + "." + strings.Repeat("b", 63) + "." + strings.Repeat("c", 63) + "." + fmt.Sprintf("%061d", i)
	}
	packet := subnetRequest(ranges, domains)
	if size := len(packet) - Header; size != 17020 || size > MaxBody {
		t.Fatalf("maximum body: got %d bytes, want 17020 within %d", size, MaxBody)
	}
	r, err := Decode(packet)
	if err != nil || len(r.Ranges) != 32 || len(r.Resolvers) != 64 {
		t.Fatalf("maximum valid request: got %d ranges, %d resolvers, error %v", len(r.Ranges), len(r.Resolvers), err)
	}
	for _, cut := range []int{Header, Header + 100, len(packet) - 1} {
		if _, err := Decode(packet[:cut]); err != ErrInvalid {
			t.Fatalf("truncated request at %d: %v", cut, err)
		}
	}
}

func TestSubnetRangesRejectInvalidPolicy(t *testing.T) {
	for name, ranges := range map[string][]string{
		"duplicate":    {"10.254.20.0/22", "10.254.20.0/22"},
		"overlap":      {"10.254.20.0/22", "10.254.21.0/24"},
		"noncanonical": {"10.254.20.1/22"},
		"public-ip":    {"8.8.8.0/24"},
		"ipv6":         {"fd00::/64"},
		"too-broad":    {"10.0.0.0/15"},
		"too-narrow":   {"10.254.20.0/29"},
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := Decode(subnetRequest(ranges, nil)); err != ErrInvalid {
				t.Fatalf("accepted invalid destination ranges %v: %v", ranges, err)
			}
		})
	}
}

func TestDisjointSubnetPacketAuthorization(t *testing.T) {
	r, err := Decode(subnetRequest([]string{"10.254.20.0/22", "10.254.60.0/24"}, nil))
	if err != nil {
		t.Fatal(err)
	}
	const local = uint32(0xc0000201)
	for _, tc := range []struct {
		name    string
		address uint32
		allowed bool
	}{
		{"private", 0x0afe1405, true},
		{"isolated", 0x0afe3c05, true},
		{"public-subnet", 0x0afe0005, false},
		{"unused-vpc-space", 0x0afe6405, false},
		{"outside-vpc", 0x0aff1405, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := PacketAllowed(tcpPacket(local, tc.address), r, local, true); got != tc.allowed {
				t.Fatalf("outbound authorization: got %v, want %v", got, tc.allowed)
			}
			if got := PacketAllowed(tcpPacket(tc.address, local), r, local, false); got != tc.allowed {
				t.Fatalf("inbound authorization: got %v, want %v", got, tc.allowed)
			}
		})
	}
}

func TestSubnetRangeExtensionCannotBroadenDestinations(t *testing.T) {
	original, err := Decode(subnetRequest([]string{"10.254.20.0/22", "10.254.60.0/24"}, []string{"db.internal"}))
	if err != nil {
		t.Fatal(err)
	}
	extended := original
	extended.Resolvers = []Resolver{{Domain: "db.internal", Port: 5300}, {Domain: "other.internal", Port: 5300}}
	if !Extension(original, extended) {
		t.Fatal("rejected DNS-only extension with unchanged destinations")
	}
	for _, replacement := range [][]netip.Prefix{
		{netip.MustParsePrefix("10.254.0.0/16")},
		{netip.MustParsePrefix("10.254.16.0/21"), netip.MustParsePrefix("10.254.60.0/24")},
		{netip.MustParsePrefix("10.254.20.0/22"), netip.MustParsePrefix("10.254.60.0/24"), netip.MustParsePrefix("10.254.0.0/24")},
	} {
		changed := extended
		changed.Ranges = replacement
		if Extension(original, changed) {
			t.Fatalf("allowed destination change during DNS extension: %v", replacement)
		}
	}
}
