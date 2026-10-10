//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func snapshotFixture() []byte {
	var b bytes.Buffer
	put := func(v any) { binary.Write(&b, binary.BigEndian, v) }
	b.WriteString("STLVSNP3")
	put(uint64(1))
	put(uint32(502))
	put(uint32(1234))
	put(uint64(100))
	put(uint64(10))
	put([8]uint32{0, 502, 20, 502, 20, 1234, 0, 1})
	put([16]byte{1})
	put([16]byte{2})
	put(uint32(1))
	put(byte(0))
	return b.Bytes()
}
func TestSnapshotHistoricalFixture(t *testing.T) {
	data := snapshotFixture()
	s, e := DecodeSnapshot(data)
	if e != nil || s.Peer.UID != 502 || s.Peer.PID != 1234 {
		t.Fatal(s, e)
	}
	encoded, e := EncodeSnapshot(s)
	if e != nil || !bytes.Equal(encoded, data) {
		t.Fatal("schema mismatch", e)
	}
	for n := 0; n < len(data); n++ {
		if _, e := DecodeSnapshot(data[:n]); e == nil {
			t.Fatal("prefix", n)
		}
	}
	if _, e := DecodeSnapshot(append(data, 0)); e == nil {
		t.Fatal("trailing data")
	}
}
func TestJournalFraming(t *testing.T) {
	s := snapshotFixture()
	p := make([]byte, len(s)+16)
	copy(p, "STLVJNL1")
	binary.BigEndian.PutUint32(p[8:], uint32(len(s)))
	binary.BigEndian.PutUint32(p[12:], checksum(s))
	copy(p[16:], s)
	if !accepted(nil, p) {
		t.Fatal("initial rejected")
	}
	for n := 0; n < len(p); n++ {
		if !incomplete(p[:n]) {
			t.Fatal("interrupted prefix", n)
		}
	}
	if incomplete(p) {
		t.Fatal("complete frame")
	}
	p[len(p)-1]++
	if _, e := journalPayload(p); e == nil {
		t.Fatal("checksum")
	}
	if incomplete([]byte("foreign")) {
		t.Fatal("foreign prefix")
	}
}
func TestResolverDomain(t *testing.T) {
	got, e := resolverDomain("arbitrary", []byte("#comment\n domain DB.Internal. # comment\n"))
	if e != nil || got != "db.internal" {
		t.Fatal(got, e)
	}
	for _, s := range []string{"domain a.internal\ndomain b.internal", "domain a.internal extra", "domain -a.internal", "nameserver 1\x00"} {
		if _, e := resolverDomain("a.internal", []byte(s)); e == nil {
			t.Fatal(s)
		}
	}
}
func TestSuccessorRejectsIdentityAndRevisionChanges(t *testing.T) {
	a, e := DecodeSnapshot(snapshotFixture())
	if e != nil {
		t.Fatal(e)
	}
	b := a.Clone()
	b.Revision++
	if Successor(a, b) {
		t.Fatal("no-op")
	}
	b.Peer.Seconds++
	if Successor(a, b) {
		t.Fatal("birth changed")
	}
	b = a.Clone()
	b.Revision += 2
	if Successor(a, b) {
		t.Fatal("skipped revision")
	}
}
func FuzzSnapshot(f *testing.F) {
	f.Add(snapshotFixture())
	f.Fuzz(func(t *testing.T, p []byte) {
		s, e := DecodeSnapshot(p)
		if e == nil {
			encoded, e := EncodeSnapshot(s)
			if e != nil || !bytes.Equal(encoded, p) {
				t.Fatal("noncanonical snapshot")
			}
		}
	})
}
