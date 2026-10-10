// Package protocol defines bounded OS-independent control and packet contracts.
package protocol

import (
	"encoding/binary"
	"errors"
	"net/netip"
	"regexp"
	"strings"
)

const (
	Header            = 56
	MaxBody           = 32768
	MaxUnits          = 8
	MaxRanges         = 8
	MaxDomains        = 64
	MaxJournal        = 512 * 1024
	IPv4       uint32 = 2 // Fixed carrier marker, independent of host socket constants.
	Inspect    byte   = 1
	Acquire    byte   = 2
	Configure  byte   = 3
	Remove     byte   = 4
	Release    byte   = 5
	Reconcile  byte   = 6
)

var Invalid = errors.New("invalid bounded protocol value")
var vpcPattern = regexp.MustCompile(`^vpc-([0-9a-f]{8}|[0-9a-f]{17})$`)

type Resolver struct {
	Domain string
	Port   uint16
}
type Request struct {
	Operation           byte
	KeepDNS             bool
	Session, Capability [16]byte
	Generation          uint64
	Unit                uint32
	VPC                 string
	Ranges              []netip.Prefix
	Resolvers           []Resolver
}

func Nonzero(value []byte) bool {
	var b byte
	for _, v := range value {
		b |= v
	}
	return b != 0
}
func Domain(value string) (string, error) {
	value = strings.ToLower(strings.TrimSuffix(value, "."))
	if len(value) == 0 || len(value) > 253 {
		return "", Invalid
	}
	for _, label := range strings.Split(value, ".") {
		if len(label) == 0 || len(label) > 63 || label[0] == '-' || label[len(label)-1] == '-' {
			return "", Invalid
		}
		for _, v := range []byte(label) {
			if !(v >= 'a' && v <= 'z' || v >= '0' && v <= '9' || v == '-') {
				return "", Invalid
			}
		}
	}
	return value, nil
}
func DNSOverlap(a, b string) bool {
	return a == b || strings.HasSuffix(a, "."+b) || strings.HasSuffix(b, "."+a)
}
func Prefix(value string) (netip.Prefix, error) {
	p, err := netip.ParsePrefix(value)
	if err != nil || !p.Addr().Is4() || p.Bits() < 16 || p.Bits() > 28 || p != p.Masked() || p.String() != value {
		return netip.Prefix{}, Invalid
	}
	for _, private := range []string{"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"} {
		if q := netip.MustParsePrefix(private); q.Bits() <= p.Bits() && q.Contains(p.Addr()) {
			return p, nil
		}
	}
	return netip.Prefix{}, Invalid
}
func Decode(packet []byte) (r Request, err error) {
	err = Invalid
	if len(packet) < Header || len(packet) > Header+MaxBody || string(packet[:8]) != "STLVTUN1" || packet[10] != 0 || packet[11] != 0 || int(binary.BigEndian.Uint32(packet[12:])) != len(packet)-Header {
		return
	}
	r.Operation = packet[8]
	r.KeepDNS = packet[9] == 1
	if r.Operation < Inspect || r.Operation > Reconcile || packet[9] > 1 || r.KeepDNS && r.Operation != Remove {
		return
	}
	copy(r.Session[:], packet[16:32])
	copy(r.Capability[:], packet[32:48])
	r.Generation = binary.BigEndian.Uint64(packet[48:])
	anonymous := r.Operation == Inspect || r.Operation == Reconcile
	scoped := r.Operation == Configure || r.Operation == Remove
	if Nonzero(r.Session[:]) == anonymous || Nonzero(r.Capability[:]) != (!anonymous && r.Operation != Acquire) || (r.Generation != 0) != scoped {
		return
	}
	body := packet[Header:]
	if scoped {
		if len(body) < 4 {
			return
		}
		r.Unit = binary.BigEndian.Uint32(body)
		body = body[4:]
		if r.Unit == 0 {
			return
		}
	}
	text := func(limit int) (string, bool) {
		if len(body) == 0 {
			return "", false
		}
		n := int(body[0])
		body = body[1:]
		if n == 0 || n >= limit || n > len(body) {
			return "", false
		}
		s := string(body[:n])
		body = body[n:]
		for _, b := range []byte(s) {
			if b == 0 || b > 127 {
				return "", false
			}
		}
		return s, true
	}
	if r.Operation == Configure {
		var ok bool
		r.VPC, ok = text(22)
		if !ok || !vpcPattern.MatchString(r.VPC) || len(body) == 0 {
			return
		}
		n := int(body[0])
		body = body[1:]
		if n == 0 || n > MaxRanges {
			return
		}
		for i := 0; i < n; i++ {
			s, ok := text(20)
			if !ok {
				return
			}
			p, e := Prefix(s)
			if e != nil {
				return
			}
			for _, q := range r.Ranges {
				if p.Overlaps(q) {
					return
				}
			}
			r.Ranges = append(r.Ranges, p)
		}
		if len(body) == 0 {
			return
		}
		n = int(body[0])
		body = body[1:]
		if n > MaxDomains {
			return
		}
		for i := 0; i < n; i++ {
			s, ok := text(254)
			if !ok {
				return
			}
			normalized, e := Domain(s)
			if e != nil || normalized != s {
				return
			}
			if _, e := netip.ParseAddr(s); e == nil {
				return
			}
			if len(body) < 2 {
				return
			}
			port := binary.BigEndian.Uint16(body)
			body = body[2:]
			if port < 1024 {
				return
			}
			for _, q := range r.Resolvers {
				if q.Domain == s {
					return
				}
			}
			r.Resolvers = append(r.Resolvers, Resolver{s, port})
		}
	}
	if len(body) != 0 {
		return
	}
	return r, nil
}
func Extension(a, b Request) bool {
	if a.Operation != Configure || b.Operation != Configure || a.Unit != b.Unit || a.Generation != b.Generation || a.KeepDNS != b.KeepDNS || a.VPC != b.VPC || a.Session != b.Session || a.Capability != b.Capability || len(a.Ranges) != len(b.Ranges) || len(b.Resolvers) <= len(a.Resolvers) || len(b.Resolvers) > MaxDomains {
		return false
	}
	for i, p := range a.Ranges {
		if p != b.Ranges[i] {
			return false
		}
	}
	for i, p := range a.Resolvers {
		if p != b.Resolvers[i] {
			return false
		}
	}
	return true
}
func PacketAllowed(ip []byte, r Request, local uint32, fromHost bool) bool {
	if len(ip) < 20 || len(ip) > 1500 || ip[0] != 0x45 || ip[9] != 6 || int(binary.BigEndian.Uint16(ip[2:])) != len(ip) || binary.BigEndian.Uint16(ip[6:])&0x3fff != 0 {
		return false
	}
	source, dest := binary.BigEndian.Uint32(ip[12:]), binary.BigEndian.Uint32(ip[16:])
	remote := source
	if fromHost {
		if source != local {
			return false
		}
		remote = dest
	} else if dest != local {
		return false
	}
	var bytes [4]byte
	binary.BigEndian.PutUint32(bytes[:], remote)
	for _, p := range r.Ranges {
		if p.Contains(netip.AddrFrom4(bytes)) {
			return true
		}
	}
	return false
}
