package protocol

import "encoding/binary"

// Frame's packet includes the four-byte carrier family marker, independent of
// the host device framing. IP policy is validated separately against the grant.
type Frame struct {
	Unit       uint32
	Generation uint64
	Packet     []byte
}

func DecodeFrame(data []byte) (Frame, error) {
	if len(data) < 36 || len(data) > 1516 {
		return Frame{}, Invalid
	}
	f := Frame{binary.BigEndian.Uint32(data), binary.BigEndian.Uint64(data[4:]), data[12:]}
	if f.Unit == 0 || f.Generation == 0 {
		return Frame{}, Invalid
	}
	return f, nil
}
