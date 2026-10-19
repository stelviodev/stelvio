package protocol

import (
	"encoding/binary"
	"testing"
)

func TestDecodeFrameBoundsAndMarker(t *testing.T) {
	data := make([]byte, 12+4+20)
	binary.BigEndian.PutUint32(data, 7)
	binary.BigEndian.PutUint64(data[4:], 9)
	binary.BigEndian.PutUint32(data[12:], IPv4)

	frame, err := DecodeFrame(data)
	if err != nil {
		t.Fatal(err)
	}
	if frame.Unit != 7 || frame.Generation != 9 || binary.BigEndian.Uint32(frame.Packet) != IPv4 {
		t.Fatalf("decoded unexpected frame: %#v", frame)
	}
	if IPv4 != 2 {
		t.Fatalf("carrier marker changed: got %d, want 2", IPv4)
	}

	invalid := [][]byte{
		data[:35],
		make([]byte, 1517),
		make([]byte, len(data)),
	}
	for i, candidate := range invalid {
		if i == 2 {
			copy(candidate, data)
			binary.BigEndian.PutUint64(candidate[4:], 0)
		}
		if _, err := DecodeFrame(candidate); err == nil {
			t.Fatalf("accepted invalid frame %d", i)
		}
	}
}
