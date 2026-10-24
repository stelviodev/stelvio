//go:build darwin || linux

package forwarder

import (
	"context"
	"encoding/binary"
	"net"
	"net/netip"
	"os"
	"syscall"
	"testing"
	"time"

	"github.com/stelviodev/traforo/internal/protocol"
)

func checksum(data []byte) uint16 {
	var sum uint32
	for len(data) >= 2 {
		sum += uint32(binary.BigEndian.Uint16(data))
		data = data[2:]
	}
	if len(data) > 0 {
		sum += uint32(data[0]) << 8
	}
	for sum>>16 != 0 {
		sum = (sum & 0xffff) + (sum >> 16)
	}
	return ^uint16(sum)
}
func syn(id uint32, generation uint64, octet byte, port uint16) []byte {
	wire := make([]byte, frameHeader+40)
	binary.BigEndian.PutUint32(wire, id)
	binary.BigEndian.PutUint64(wire[4:], generation)
	binary.BigEndian.PutUint32(wire[12:], 2)
	ip := wire[frameHeader:]
	ip[0] = 0x45
	binary.BigEndian.PutUint16(ip[2:], 40)
	ip[8] = 64
	ip[9] = 6
	copy(ip[12:], []byte{192, 0, 2, 1})
	copy(ip[16:], []byte{10, octet, 0, 10})
	binary.BigEndian.PutUint16(ip[10:], checksum(ip[:20]))
	tcp := ip[20:]
	binary.BigEndian.PutUint16(tcp, port)
	binary.BigEndian.PutUint16(tcp[2:], 27017)
	binary.BigEndian.PutUint32(tcp[4:], 1)
	tcp[12] = 0x50
	tcp[13] = 2
	binary.BigEndian.PutUint16(tcp[14:], 65535)
	pseudo := append([]byte(nil), ip[12:20]...)
	pseudo = append(pseudo, 0, 6, 0, 20)
	pseudo = append(pseudo, tcp...)
	binary.BigEndian.PutUint16(tcp[16:], checksum(pseudo))
	return wire
}
func pair(t *testing.T) (*net.UnixConn, *net.UnixConn) {
	t.Helper()
	descriptors, err := syscall.Socketpair(syscall.AF_UNIX, syscall.SOCK_DGRAM, 0)
	if err != nil {
		t.Fatal(err)
	}
	result := []*net.UnixConn{}
	for _, fd := range descriptors {
		file := os.NewFile(uintptr(fd), "fixture")
		c, err := net.FileConn(file)
		closeErr := file.Close()
		if err != nil {
			t.Fatal(err)
		}
		if closeErr != nil {
			_ = c.Close()
			t.Fatal(closeErr)
		}
		result = append(result, c.(*net.UnixConn))
	}
	return result[0], result[1]
}
func TestCarrierDemultiplexingAndIndependentGenerations(t *testing.T) {
	a, b := pair(t)
	t.Cleanup(func() {
		_ = a.Close() // Test teardown is best-effort after assertions complete.
		_ = b.Close()
	})
	m := &manager{units: make(map[uint32]*unit), generations: make(map[uint32]uint64), carrier: a}
	defer func() {
		for _, u := range m.units {
			u.close()
		}
	}()
	go m.dispatch()
	for _, c := range []command{
		{Operation: "add", Unit: 1, Generation: 7, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}},
		{Operation: "add", Unit: 2, Generation: 9, Attempt: 1, Proxy: "127.0.0.1:1081", CIDRs: []string{"10.253.0.0/16"}},
	} {
		if err := m.apply(c); err != nil {
			t.Fatal(err)
		}
	}
	check := func(id uint32, generation uint64, octet byte, port uint16) {
		t.Helper()
		if err := b.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
			t.Fatal(err)
		}
		if _, err := b.Write(syn(id, generation, octet, port)); err != nil {
			t.Fatal(err)
		}
		buffer := make([]byte, 2048)
		for {
			n, err := b.Read(buffer)
			if err != nil {
				t.Fatal(err)
			}
			if n < 56 {
				t.Fatal("short carrier reply")
			}
			actual := binary.BigEndian.Uint32(buffer)
			if (actual == 1 && buffer[29] != 254) || (actual == 2 && buffer[29] != 253) {
				t.Fatal("cross-VPC return frame")
			}
			// Unacknowledged earlier fixture SYNs may retransmit normally.
			if actual != id || binary.BigEndian.Uint16(buffer[38:]) != port {
				continue
			}
			if binary.BigEndian.Uint64(buffer[4:]) != generation || binary.BigEndian.Uint32(buffer[12:]) != 2 || buffer[49]&0x12 != 0x12 || binary.BigEndian.Uint32(buffer[44:]) != 2 {
				t.Fatalf("invalid framed SYN ACK: %x", buffer[:n])
			}
			return
		}
	}
	check(1, 7, 254, 45001)
	check(2, 9, 253, 45002)
	if m.apply(command{Operation: "remove", Unit: 1, Generation: 7, Attempt: 2}) == nil {
		t.Fatal("stale removal accepted")
	}
	check(1, 7, 254, 45003)
	if err := m.apply(command{Operation: "remove", Unit: 1, Generation: 7, Attempt: 1}); err != nil {
		t.Fatal(err)
	}
	check(2, 9, 253, 45004)
	if err := m.apply(command{Operation: "add", Unit: 1, Generation: 8, Attempt: 2, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}); err != nil {
		t.Fatal(err)
	}
	check(1, 8, 254, 45005)
	if m.apply(command{Operation: "add", Unit: 3, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}) == nil {
		t.Fatal("overlap accepted")
	}
	check(2, 9, 253, 45006)
}

func TestRemovedUnitFencesOldGenerationAndAttempt(t *testing.T) {
	a, b := pair(t)
	m := &manager{carrier: a}
	t.Cleanup(func() {
		m.close()
		_ = a.Close()
		_ = b.Close()
	})
	add := command{Operation: "add", Unit: 1, Generation: 7, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}
	if err := m.apply(add); err != nil {
		t.Fatal(err)
	}
	if err := m.apply(command{Operation: "remove", Unit: 1, Generation: 7, Attempt: 1}); err != nil {
		t.Fatal(err)
	}
	if err := m.apply(command{Operation: "remove", Unit: 1, Generation: 8, Attempt: 1}); err == nil {
		t.Fatal("remove with a stale unit generation was accepted")
	}
	add.Attempt = 2
	if err := m.apply(add); err == nil {
		t.Fatal("add with a reused unit generation was accepted")
	}
	add.Generation = 8
	if err := m.apply(add); err != nil {
		t.Fatalf("newer attempt with an advanced generation rejected: %v", err)
	}
}

func TestQueuedOldGenerationFrameIsDroppedAfterReconnect(t *testing.T) {
	carrier, peer := pair(t)
	m := &manager{carrier: carrier}
	var detached []*unit
	dispatchDone := make(chan struct{})
	dispatchStarted := false
	t.Cleanup(func() {
		m.close()
		for _, u := range detached {
			u.close()
		}
		_ = carrier.Close()
		_ = peer.Close()
		if dispatchStarted {
			<-dispatchDone
		}
	})
	if err := m.apply(command{Operation: "add", Unit: 1, Generation: 7, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}); err != nil {
		t.Fatal(err)
	}
	old := syn(1, 7, 254, 45010)
	if _, err := peer.Write(old); err != nil {
		t.Fatal(err)
	}
	if err := m.apply(command{Operation: "remove", Unit: 1, Generation: 7, Attempt: 1}); err != nil {
		t.Fatal(err)
	}
	if err := m.apply(command{Operation: "add", Unit: 1, Generation: 7, Attempt: 2, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}); err == nil {
		t.Fatal("reconnect reused the old carrier generation")
	}
	if err := m.apply(command{Operation: "add", Unit: 1, Generation: 8, Attempt: 2, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}); err != nil {
		t.Fatal(err)
	}
	stacked := m.units[1]
	detached = append(detached, stacked)
	ctx, cancel := context.WithCancel(context.Background())
	unit := &unit{id: 1, generation: 8, cidrs: stacked.cidrs, packets: make(chan []byte, 2), ctx: ctx, cancel: cancel, connections: make(map[net.Conn]struct{})}
	m.units[1] = unit
	go func() { defer close(dispatchDone); m.dispatch() }()
	dispatchStarted = true
	if _, err := peer.Write(syn(1, 8, 254, 45011)); err != nil {
		t.Fatal(err)
	}
	select {
	case packet := <-unit.packets:
		if got := binary.BigEndian.Uint16(packet[20:22]); got != 45011 {
			t.Fatalf("queued old-attempt packet reached the new unit: source port %d", got)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("new-generation packet was not dispatched")
	}
}

func TestUnitIdentityHistoryIsBounded(t *testing.T) {
	a, b := pair(t)
	m := &manager{carrier: a}
	t.Cleanup(func() {
		m.close()
		_ = a.Close()
		_ = b.Close()
	})
	for id := uint32(1); id <= protocol.MaxUnits; id++ {
		c := command{Operation: "add", Unit: id, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}
		if err := m.apply(c); err != nil {
			t.Fatalf("add unit %d: %v", id, err)
		}
		c.Operation = "remove"
		c.Proxy = ""
		c.CIDRs = nil
		if err := m.apply(c); err != nil {
			t.Fatalf("remove unit %d: %v", id, err)
		}
	}
	if len(m.unitGenerations) != protocol.MaxUnits {
		t.Fatalf("retained %d unit identities, want %d", len(m.unitGenerations), protocol.MaxUnits)
	}
	if err := m.apply(command{Operation: "add", Unit: 1, Generation: 2, Attempt: 2, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}); err != nil {
		t.Fatalf("reconnect known unit at capacity: %v", err)
	}
	if err := m.apply(command{Operation: "add", Unit: protocol.MaxUnits + 1, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.253.0.0/16"}}); err == nil {
		t.Fatal("new unit identity exceeded the bounded history")
	}
	if len(m.unitGenerations) != protocol.MaxUnits {
		t.Fatalf("rejected identity changed history size to %d", len(m.unitGenerations))
	}
}

func TestDispatchRejectsMalformedFramesAndAncillaryDescriptors(t *testing.T) {
	carrier, peer := pair(t)
	ctx, cancel := context.WithCancel(context.Background())
	// This test observes dispatch's queue directly. A live stack would race
	// the assertion by consuming the packet from that same queue.
	u := &unit{id: 1, generation: 7, cidrs: []netip.Prefix{netip.MustParsePrefix("10.254.0.0/16")}, packets: make(chan []byte, 16), ctx: ctx, cancel: cancel}
	m := &manager{carrier: carrier, units: map[uint32]*unit{1: u}}
	dispatchDone := make(chan struct{})
	go func() { defer close(dispatchDone); m.dispatch() }()
	t.Cleanup(func() {
		m.close()
		_ = carrier.Close()
		_ = peer.Close()
		<-dispatchDone
	})

	invalid := [][]byte{}
	badMarker := syn(1, 7, 254, 45001)
	binary.BigEndian.PutUint32(badMarker[12:], 9)
	invalid = append(invalid, badMarker)
	invalid = append(invalid, syn(1, 8, 254, 45002))
	invalid = append(invalid, syn(1, 7, 253, 45003))
	fragmented := syn(1, 7, 254, 45004)
	binary.BigEndian.PutUint16(fragmented[frameHeader+6:], 0x2000)
	invalid = append(invalid, fragmented)
	badTCPHeader := syn(1, 7, 254, 45005)
	badTCPHeader[frameHeader+32] = 0x40
	invalid = append(invalid, badTCPHeader)
	invalid = append(invalid, make([]byte, frameHeader+mtu+1))
	for _, wire := range invalid {
		if _, err := peer.Write(wire); err != nil {
			t.Fatal(err)
		}
	}

	rightsFile, err := os.Open("/dev/null")
	if err != nil {
		t.Fatal(err)
	}
	rightsFrame := syn(1, 7, 254, 45006)
	if _, _, err = peer.WriteMsgUnix(rightsFrame, syscall.UnixRights(int(rightsFile.Fd())), nil); err != nil {
		_ = rightsFile.Close()
		t.Fatal(err)
	}
	_ = rightsFile.Close()

	valid := syn(1, 7, 254, 45007)
	if _, err := peer.Write(valid); err != nil {
		t.Fatal(err)
	}
	select {
	case packet := <-u.packets:
		if string(packet) != string(valid[frameHeader:]) {
			t.Fatalf("invalid packet reached the stack before valid frame: %x", packet)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("valid packet was not dispatched")
	}
}
