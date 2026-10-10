// Package protocol preserves the opaque Darwin owner receipt in snapshot v3.
// It contains no live process authority or file/handle values. Future platforms
// require their own native peer validation and, for Windows, a new owner schema.
package protocol

type DarwinOwner struct {
	UID             uint32
	PID             int32
	Seconds, Micros uint64
	Token           [8]uint32
}

func (p DarwinOwner) ValidV3() bool {
	return p.UID != 0 && p.PID > 0 && p.Seconds != 0 && p.Micros < 1000000 && p.Token[1] == p.UID && p.Token[3] == p.UID && p.Token[5] == uint32(p.PID)
}
