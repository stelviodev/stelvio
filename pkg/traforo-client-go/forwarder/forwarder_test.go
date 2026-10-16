package forwarder

import (
	"encoding/binary"
	"net"
	"os"
	"syscall"
	"testing"
	"time"
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
		file.Close()
		if err != nil {
			t.Fatal(err)
		}
		result = append(result, c.(*net.UnixConn))
	}
	return result[0], result[1]
}
func TestCarrierDemultiplexingAndIndependentGenerations(t *testing.T) {
	a, b := pair(t)
	defer a.Close()
	defer b.Close()
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
		b.SetDeadline(time.Now().Add(2 * time.Second))
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
	if err := m.apply(command{Operation: "add", Unit: 1, Generation: 7, Attempt: 2, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}); err != nil {
		t.Fatal(err)
	}
	check(1, 7, 254, 45005)
	if m.apply(command{Operation: "add", Unit: 3, Generation: 1, Attempt: 1, Proxy: "127.0.0.1:1080", CIDRs: []string{"10.254.0.0/16"}}) == nil {
		t.Fatal("overlap accepted")
	}
	check(2, 9, 253, 45006)
}
