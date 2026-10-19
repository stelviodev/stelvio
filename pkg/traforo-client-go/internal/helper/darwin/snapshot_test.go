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

func configurePacket(generation uint64, domains ...string) []byte {
	var body bytes.Buffer
	_ = binary.Write(&body, binary.BigEndian, uint32(7))
	writeText := func(value string) {
		body.WriteByte(byte(len(value)))
		body.WriteString(value)
	}
	writeText("vpc-12345678")
	body.WriteByte(1)
	writeText("10.254.0.0/16")
	body.WriteByte(byte(len(domains)))
	for _, domain := range domains {
		writeText(domain)
		_ = binary.Write(&body, binary.BigEndian, uint16(5300))
	}
	packet := make([]byte, protocol.Header+body.Len())
	copy(packet, "STLVTUN1")
	packet[8] = protocol.Configure
	binary.BigEndian.PutUint32(packet[12:], uint32(body.Len()))
	packet[16] = 1
	packet[32] = 2
	binary.BigEndian.PutUint64(packet[48:], generation)
	copy(packet[protocol.Header:], body.Bytes())
	return packet
}

func TestRetainedUnitCanJournalHigherGenerationWithoutDroppingDNSClaims(t *testing.T) {
	oldPacket := configurePacket(1, "db.internal")
	oldRequest, err := protocol.Decode(oldPacket)
	if err != nil {
		t.Fatal("old request", err)
	}
	newPacket := configurePacket(3, "db.internal", "cache.internal")
	newRequest, err := protocol.Decode(newPacket)
	if err != nil {
		t.Fatal("new request", err)
	}
	a, err := DecodeSnapshot(snapshotFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	a.Revision = 10
	a.Units = []Unit{{
		Phase: Retained, KeepDNS: true, Generation: 2, Index: 4,
		Packet: oldPacket, Request: oldRequest,
		Files: []Receipt{{Phase: 2, Device: 22, Inode: 33}},
	}}
	if err = a.Validate(); err != nil {
		t.Fatal("retained state", err)
	}
	b := a.Clone()
	b.Revision++
	b.Units[0] = Unit{
		Phase: Preparing, Generation: 3, Index: 5, Packet: newPacket, Request: newRequest,
		Files: make([]Receipt, len(newRequest.Resolvers)),
		Previous: &ResolverHistory{
			Packet: oldPacket,
			Files:  []Receipt{{Phase: 2, Device: 22, Inode: 33}},
		},
	}
	if err = b.Validate(); err != nil {
		t.Fatal("reactivation state", err)
	}
	if !Successor(a, b) {
		t.Fatal("retained unit refused higher-generation journal transition")
	}
	wire, err := EncodeSnapshot(b)
	if err != nil || string(wire[:8]) != "STLVSNP5" {
		t.Fatal("versioned snapshot", err)
	}
	decoded, err := DecodeSnapshot(wire)
	if err != nil || !reflect.DeepEqual(decoded, b) {
		t.Fatal("reactivation snapshot round trip", err)
	}
	for n := 0; n < len(wire); n++ {
		if _, err := DecodeSnapshot(wire[:n]); err == nil {
			t.Fatal("accepted truncated reactivation snapshot", n)
		}
	}
	if _, err = DecodeSnapshot(append(append([]byte(nil), wire...), 0)); err == nil {
		t.Fatal("accepted trailing reactivation data")
	}
	oldClaims := b.Clone()
	oldClaims.Units[0].Packet = configurePacket(3, "cache.internal")
	if oldClaims.Validate() == nil {
		t.Fatal("new generation dropped a retained resolver claim")
	}
	changedPort := b.Clone()
	changedPort.Units[0].Packet = configurePacketWithPorts(3, []protocol.Resolver{{Domain: "db.internal", Port: 5301}, {Domain: "cache.internal", Port: 5300}})
	if changedPort.Validate() == nil {
		t.Fatal("new generation changed a retained resolver port")
	}
	stale := b.Clone()
	stale.Units[0].Generation = 2
	if stale.Validate() == nil || Successor(a, stale) {
		t.Fatal("stale generation accepted")
	}

	p1 := b.Clone()
	p1.Revision++
	p1.Units[0].Files[0] = Receipt{Phase: 1, Device: 44, Inode: 55}
	if !Successor(b, p1) {
		t.Fatal("staging first resolver rejected")
	}
	p2 := p1.Clone()
	p2.Revision++
	p2.Units[0].Files[0].Phase = 2
	if !Successor(p1, p2) {
		t.Fatal("publishing first resolver rejected")
	}
	p3 := p2.Clone()
	p3.Revision++
	p3.Units[0].Files[1] = Receipt{Phase: 1, Device: 44, Inode: 56}
	if !Successor(p2, p3) {
		t.Fatal("staging second resolver rejected")
	}
	p4 := p3.Clone()
	p4.Revision++
	p4.Units[0].Files[1].Phase = 2
	if !Successor(p3, p4) {
		t.Fatal("publishing second resolver rejected")
	}
	c := p4.Clone()
	c.Revision++
	c.Units[0].Phase = Active
	if !Successor(p4, c) {
		t.Fatal("completed new generation rejected")
	}
	d := c.Clone()
	d.Revision++
	d.Units[0].Previous.Files[0] = Receipt{}
	if !Successor(c, d) {
		t.Fatal("old resolver retirement journal rejected")
	}
	f := d.Clone()
	f.Revision++
	f.Units[0].Previous = nil
	if !Successor(d, f) {
		t.Fatal("completed resolver retirement rejected")
	}
}

func TestReactivationJournalRecoveryClassification(t *testing.T) {
	retained, err := DecodeSnapshot(snapshotFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	oldPacket := configurePacket(1, "db.internal")
	oldRequest, err := protocol.Decode(oldPacket)
	if err != nil {
		t.Fatal(err)
	}
	retained.Revision = 10
	retained.Units = []Unit{{
		Phase: Retained, KeepDNS: true, Generation: 2, Index: 4, Packet: oldPacket,
		Request: oldRequest, Files: []Receipt{{Phase: 2, Device: 22, Inode: 33}},
	}}
	reactivation := retained.Clone()
	reactivation.Revision++
	reactivationPacket := configurePacket(3, "db.internal", "cache.internal")
	newRequest, err := protocol.Decode(reactivationPacket)
	if err != nil {
		t.Fatal(err)
	}
	reactivation.Units[0] = Unit{
		Phase: Preparing, Generation: 3, Index: 5, Packet: reactivationPacket, Request: newRequest,
		Files:    make([]Receipt, len(newRequest.Resolvers)),
		Previous: &ResolverHistory{Packet: oldPacket, Files: []Receipt{{Phase: 2, Device: 22, Inode: 33}}},
	}
	frame := func(snapshot Snapshot) []byte {
		t.Helper()
		payload, err := EncodeSnapshot(snapshot)
		if err != nil {
			t.Fatal(err)
		}
		wire := make([]byte, 16+len(payload))
		copy(wire, "STLVJNL1")
		binary.BigEndian.PutUint32(wire[8:], uint32(len(payload)))
		binary.BigEndian.PutUint32(wire[12:], checksum(payload))
		copy(wire[16:], payload)
		return wire
	}
	old, next := frame(retained), frame(reactivation)
	if !accepted(old, next) || classifyRecovery(old, next) != recoveryPromote {
		t.Fatal("complete generation-advance journal was not recoverable")
	}
	if classifyRecovery(old, next[:len(next)-1]) != recoveryDiscard {
		t.Fatal("interrupted generation-advance journal was not safely discarded")
	}
	corrupt := append([]byte(nil), next...)
	corrupt[len(corrupt)-1] ^= 1
	if classifyRecovery(old, corrupt) != recoveryUncertain {
		t.Fatal("corrupt generation-advance journal did not retain uncertainty")
	}
}

func configurePacketWithPorts(generation uint64, resolvers []protocol.Resolver) []byte {
	var body bytes.Buffer
	_ = binary.Write(&body, binary.BigEndian, uint32(7))
	writeText := func(value string) {
		body.WriteByte(byte(len(value)))
		body.WriteString(value)
	}
	writeText("vpc-12345678")
	body.WriteByte(1)
	writeText("10.254.0.0/16")
	body.WriteByte(byte(len(resolvers)))
	for _, resolver := range resolvers {
		writeText(resolver.Domain)
		_ = binary.Write(&body, binary.BigEndian, resolver.Port)
	}
	packet := make([]byte, protocol.Header+body.Len())
	copy(packet, "STLVTUN1")
	packet[8] = protocol.Configure
	binary.BigEndian.PutUint32(packet[12:], uint32(body.Len()))
	packet[16] = 1
	packet[32] = 2
	binary.BigEndian.PutUint64(packet[48:], generation)
	copy(packet[protocol.Header:], body.Bytes())
	return packet
}
