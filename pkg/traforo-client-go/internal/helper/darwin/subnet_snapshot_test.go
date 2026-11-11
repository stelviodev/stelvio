//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"fmt"
	"strings"
	"testing"

	"github.com/stelviodev/traforo/internal/protocol"
)

// Write an independent request body; never use the snapshot encoder to generate
// expected request bytes. Every unit has disjoint ranges and DNS ownership.
func maximumSubnetPacket(unit uint32, generation uint64) []byte {
	body := make([]byte, 4)
	binary.BigEndian.PutUint32(body, unit)
	text := func(s string) { body = append(body, byte(len(s))); body = append(body, s...) }
	text(fmt.Sprintf("vpc-%017x", unit))
	body = append(body, 32)
	for i := 0; i < 32; i++ {
		text(fmt.Sprintf("192.168.%d.%d/28", 100+int(unit)*4+i/8, 128+(i%8)*16))
	}
	body = append(body, 64)
	for i := 0; i < 64; i++ {
		text(strings.Repeat("a", 63) + "." + strings.Repeat("b", 63) + "." + strings.Repeat("c", 63) + "." + fmt.Sprintf("%061d", int(unit)*64+i))
		body = append(body, 0x14, 0xb4)
	}
	packet := make([]byte, protocol.Header)
	copy(packet, "STLVTUN1")
	packet[8] = protocol.Configure
	packet[16] = 1
	packet[32] = 2
	binary.BigEndian.PutUint32(packet[12:], uint32(len(body)))
	binary.BigEndian.PutUint64(packet[48:], generation)
	return append(packet, body...)
}
func maximumSubnetSnapshot(t testing.TB) Snapshot {
	t.Helper()
	state, err := DecodeSnapshot(snapshotFixture(t))
	if err != nil {
		t.Fatal(err)
	}
	state.Revision = 100
	for unit := uint32(1); unit <= 8; unit++ {
		packet := maximumSubnetPacket(unit, 3)
		request, err := protocol.Decode(packet)
		if err != nil {
			t.Fatal(err)
		}
		previous := maximumSubnetPacket(unit, 1)
		files := make([]Receipt, 64)
		for i := range files {
			files[i] = Receipt{Phase: 2, Device: 22, Inode: uint64(unit)*100 + uint64(i) + 1}
		}
		state.Units = append(state.Units, Unit{Phase: Preparing, Generation: 3, Index: unit + 40, Packet: packet, Request: request, Files: make([]Receipt, 64), Previous: &ResolverHistory{Packet: previous, Files: files}})
	}
	return state
}
func frameSnapshot(t testing.TB, snapshot Snapshot) []byte {
	t.Helper()
	payload, err := EncodeSnapshot(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	data := make([]byte, 16)
	copy(data, "STLVJNL1")
	binary.BigEndian.PutUint32(data[8:], uint32(len(payload)))
	binary.BigEndian.PutUint32(data[12:], checksum(payload))
	return append(data, payload...)
}
func TestMaximumSubnetJournalCapacityAndRecovery(t *testing.T) {
	before := maximumSubnetSnapshot(t)
	payload, err := EncodeSnapshot(before)
	if err != nil {
		t.Fatal(err)
	}
	// Eight units, each with 32 longest prefixes, 64 longest resolver names,
	// and both current and previous-generation packets and resolver receipts.
	if len(payload) != 290941 || len(payload) >= protocol.MaxJournal {
		t.Fatalf("maximum snapshot %d exceeds %d", len(payload), protocol.MaxJournal)
	}
	after, err := DecodeSnapshot(payload)
	if err != nil {
		t.Fatal(err)
	}
	if len(after.Units) != 8 {
		t.Fatal("unit loss")
	}
	for _, unit := range after.Units {
		if len(unit.Request.Ranges) != 32 || len(unit.Request.Resolvers) != 64 || unit.Previous == nil {
			t.Fatal("destination/history loss")
		}
	}
	encoded, err := EncodeSnapshot(after)
	if err != nil || !bytes.Equal(payload, encoded) {
		t.Fatal("snapshot roundtrip", err)
	}
	// One durable receipt transition, with all other units unchanged.
	next := after.Clone()
	next.Revision++
	next.Units[0].Files[0] = Receipt{Phase: 1, Device: 23, Inode: 9001}
	oldFrame, nextFrame := frameSnapshot(t, after), frameSnapshot(t, next)
	if classifyRecovery(oldFrame, nextFrame) != recoveryPromote {
		t.Fatal("complete successor rejected")
	}
	if classifyRecovery(oldFrame, nextFrame[:len(nextFrame)-1]) != recoveryDiscard {
		t.Fatal("interrupted successor not discarded")
	}
	corrupt := append([]byte(nil), nextFrame...)
	corrupt[len(corrupt)-1] ^= 1
	if classifyRecovery(oldFrame, corrupt) != recoveryUncertain {
		t.Fatal("corruption lost uncertainty")
	}
	if _, err := DecodeSnapshot(make([]byte, protocol.MaxJournal+1)); err == nil {
		t.Fatal("oversized snapshot accepted")
	}
	t.Logf("maximum snapshot: %d bytes; journal frame: %d; limit: %d", len(payload), len(oldFrame), protocol.MaxJournal)
}
func TestManySubnetClaimsRemainFencedAcrossUnits(t *testing.T) {
	state := maximumSubnetSnapshot(t)
	second := state.Units[1].Packet
	first := state.Units[0].Packet
	// Copy one destination from unit 1 into unit 2, preserving its unit/VPC IDs.
	// Locate the first range after the fixed unit and length-prefixed VPC ID.
	offset := protocol.Header + 4 + 1 + int(second[protocol.Header+4]) + 1
	size := int(second[offset]) + 1
	firstOffset := protocol.Header + 4 + 1 + int(first[protocol.Header+4]) + 1
	changed := append([]byte(nil), second...)
	copy(changed[offset:offset+size], first[firstOffset:firstOffset+size])
	state.Units[1].Packet = changed
	if _, err := EncodeSnapshot(state); err == nil {
		t.Fatal("cross-unit overlapping subnet accepted")
	}
}
