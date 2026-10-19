//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"reflect"
	"testing"

	"github.com/stelviodev/traforo/internal/protocol"
)

func snapshotFixture(t testing.TB) []byte {
	t.Helper()
	var b bytes.Buffer
	put := func(v any) {
		if err := binary.Write(&b, binary.BigEndian, v); err != nil {
			t.Fatal(err)
		}
	}
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
func TestSnapshotIndependentFixture(t *testing.T) {
	data := snapshotFixture(t)
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
	s := snapshotFixture(t)
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

func TestJournalRecoveryClassification(t *testing.T) {
	payload := snapshotFixture(t)
	valid := make([]byte, 16+len(payload))
	copy(valid, "STLVJNL1")
	binary.BigEndian.PutUint32(valid[8:], uint32(len(payload)))
	binary.BigEndian.PutUint32(valid[12:], checksum(payload))
	copy(valid[16:], payload)

	if got := classifyRecovery(nil, nil); got != recoveryNone {
		t.Fatalf("missing next journal action = %d, want none", got)
	}
	if got := classifyRecovery(nil, valid); got != recoveryPromote {
		t.Fatalf("valid initial journal action = %d, want promote", got)
	}
	for n := 0; n < len(valid); n++ {
		if got := classifyRecovery(nil, valid[:n]); got != recoveryDiscard {
			t.Fatalf("incomplete journal prefix %d action = %d, want discard", n, got)
		}
	}

	corrupt := append([]byte(nil), valid...)
	corrupt[len(corrupt)-1] ^= 1
	if got := classifyRecovery(nil, corrupt); got != recoveryUncertain {
		t.Fatalf("complete corrupt journal action = %d, want uncertain", got)
	}
	if got := classifyRecovery([]byte("foreign journal"), valid[:12]); got != recoveryUncertain {
		t.Fatalf("incomplete journal with corrupt prior state action = %d, want uncertain", got)
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
	a, e := DecodeSnapshot(snapshotFixture(t))
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
	f.Add(snapshotFixture(f))
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

func TestSnapshotParentFence(t *testing.T) {
	s, e := DecodeSnapshot(snapshotFixture(t))
	if e != nil {
		t.Fatal(e)
	}
	s.Parent = protocol.DarwinParent{UID: s.Peer.UID, PID: 456, Seconds: 123, Micros: 789}
	encoded, e := EncodeSnapshot(s)
	if e != nil {
		t.Fatal(e)
	}
	if string(encoded[:8]) != "STLVSNP4" {
		t.Fatal("missing parent schema version")
	}
	decoded, e := DecodeSnapshot(encoded)
	if e != nil || !reflect.DeepEqual(decoded, s) {
		t.Fatal(e)
	}
	for n := 8; n < len(encoded); n++ {
		if _, e := DecodeSnapshot(encoded[:n]); e == nil {
			t.Fatal("accepted truncated parent snapshot", n)
		}
	}
	changed := s.Clone()
	changed.Revision++
	changed.Parent.Micros++
	if Successor(s, changed) {
		t.Fatal("parent identity changed within a lease")
	}
}
