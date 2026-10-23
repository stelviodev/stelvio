package protocol

import (
	"encoding/binary"
	"net/netip"
	"testing"
)

func request(operation byte) []byte {
	p := make([]byte, Header)
	copy(p, "STLVTUN1")
	p[8] = operation
	return p
}
func configure() []byte {
	p := request(Configure)
	p[16] = 1
	p[32] = 2
	binary.BigEndian.PutUint64(p[48:], 3)
	body := []byte{0x12, 0x34, 0x56, 0x78}
	text := func(s string) { body = append(body, byte(len(s))); body = append(body, s...) }
	text("vpc-12345678")
	body = append(body, 1)
	text("10.254.0.0/16")
	body = append(body, 1)
	text("db.example.internal")
	body = append(body, 0x14, 0xb4)
	binary.BigEndian.PutUint32(p[12:], uint32(len(body)))
	return append(p, body...)
}
func TestIndependentWire(t *testing.T) {
	p := request(Inspect)
	if r, e := Decode(p); e != nil || r.Operation != Inspect {
		t.Fatal(e)
	}
	for _, offset := range []int{0, 9, 10, 11, 15, 16, 32, 48} {
		v := append([]byte(nil), p...)
		v[offset]++
		if _, e := Decode(v); e == nil {
			t.Fatal("malformed frame", offset)
		}
	}
}
func TestConfigureIndependentWire(t *testing.T) {
	p := configure()
	r, e := Decode(p)
	if e != nil || r.Unit != 0x12345678 || r.Generation != 3 || r.Resolvers[0].Port != 5300 {
		t.Fatal(r, e)
	}
	for n := 0; n < len(p); n++ {
		if _, e := Decode(p[:n]); e == nil {
			t.Fatal("prefix", n)
		}
	}
	p[len(p)-2] = 0
	if _, e := Decode(p); e == nil {
		t.Fatal("privileged DNS port")
	}
}
func TestRangesAndDomains(t *testing.T) {
	for _, s := range []string{"10.0.0.1/16", "8.8.0.0/16", "10.0.0.0/15", "192.168.1.0/29", "10.0.0.0/016"} {
		if _, e := Prefix(s); e == nil {
			t.Fatal(s)
		}
	}
	for _, s := range []string{"-a.test", "a..test", "a_.test", "a.test\x00"} {
		if _, e := Domain(s); e == nil {
			t.Fatal(s)
		}
	}
	if !DNSOverlap("a.db.internal", "db.internal") || DNSOverlap("adb.internal", "db.internal") {
		t.Fatal("suffix boundaries")
	}
}

func TestDecodeRejectsTrailingAndOversizedBodies(t *testing.T) {
	withTrailing := append(request(Inspect), 0)
	binary.BigEndian.PutUint32(withTrailing[12:], 1)
	if _, err := Decode(withTrailing); err != ErrInvalid {
		t.Fatalf("trailing byte: got error %v", err)
	}

	oversized := request(Inspect)
	oversized = append(oversized, make([]byte, MaxBody+1)...)
	binary.BigEndian.PutUint32(oversized[12:], uint32(len(oversized)-Header))
	if _, err := Decode(oversized); err != ErrInvalid {
		t.Fatalf("oversized body: got error %v", err)
	}
}

func TestPacketAllowed(t *testing.T) {
	const local = uint32(0xc0000201)
	const remote = uint32(0x0afe0105)
	r := Request{Ranges: []netip.Prefix{netip.MustParsePrefix("10.254.0.0/16")}}

	if !PacketAllowed(tcpPacket(remote, local), r, local, false) {
		t.Fatal("rejected authorized packet to the host")
	}
	if !PacketAllowed(tcpPacket(local, remote), r, local, true) {
		t.Fatal("rejected authorized packet from the host")
	}
	if PacketAllowed(tcpPacket(0x08080808, local), r, local, false) {
		t.Fatal("accepted destination outside the authorized range")
	}
	if PacketAllowed(tcpPacket(remote, 0xc0000202), r, local, false) {
		t.Fatal("accepted packet for another local address")
	}

	malformed := tcpPacket(remote, local)
	malformed[0] = 0x46
	if PacketAllowed(malformed, r, local, false) {
		t.Fatal("accepted non-minimal IPv4 header")
	}
	malformed = tcpPacket(remote, local)
	malformed[9] = 17
	if PacketAllowed(malformed, r, local, false) {
		t.Fatal("accepted non-TCP packet")
	}
	malformed = tcpPacket(remote, local)
	binary.BigEndian.PutUint16(malformed[6:], 0x2000)
	if PacketAllowed(malformed, r, local, false) {
		t.Fatal("accepted fragmented packet")
	}
}

func tcpPacket(source, destination uint32) []byte {
	packet := make([]byte, 20)
	packet[0], packet[9] = 0x45, 6
	binary.BigEndian.PutUint16(packet[2:], uint16(len(packet)))
	binary.BigEndian.PutUint32(packet[12:], source)
	binary.BigEndian.PutUint32(packet[16:], destination)
	return packet
}

func FuzzRequest(f *testing.F) {
	f.Add(request(Inspect))
	f.Add(configure())
	f.Add(subnetRequest(subnetRanges(20), nil))
	f.Add(subnetRequest(subnetRanges(32), nil))
	f.Add(subnetRequest(subnetRanges(33), nil))
	f.Fuzz(func(t *testing.T, p []byte) {
		r, e := Decode(p)
		if e == nil && (r.Operation < Inspect || r.Operation > Reconcile || len(p) > Header+MaxBody) {
			t.Fatal("invalid accepted request")
		}
	})
}
