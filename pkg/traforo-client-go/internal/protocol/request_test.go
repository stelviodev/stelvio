package protocol

import (
	"encoding/binary"
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
func FuzzRequest(f *testing.F) {
	f.Add(request(Inspect))
	f.Add(configure())
	f.Fuzz(func(t *testing.T, p []byte) {
		r, e := Decode(p)
		if e == nil && (r.Operation < Inspect || r.Operation > Reconcile || len(p) > Header+MaxBody) {
			t.Fatal("invalid accepted request")
		}
	})
}
