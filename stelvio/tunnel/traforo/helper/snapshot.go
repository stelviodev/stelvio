package helper

import (
	"bytes"
	"encoding/binary"
	"github.com/stelviodev/traforo/protocol"
	"math"
)

const (
	Preparing byte = 1
	Active    byte = 2
	Removing  byte = 3
	Retained  byte = 4
	Removed   byte = 5
)

type Receipt struct {
	Phase         byte
	Device, Inode uint64
}
type Unit struct {
	Phase      byte
	KeepDNS    bool
	Generation uint64
	Index      uint32
	Packet     []byte
	Request    protocol.Request
	Files      []Receipt
}
type Snapshot struct {
	Revision            uint64
	Peer                protocol.DarwinOwner
	Session, Capability [16]byte
	Carrier             uint32
	Units               []Unit
}

func (s Snapshot) Clone() Snapshot {
	n := s
	n.Units = append([]Unit(nil), s.Units...)
	for i := range n.Units {
		n.Units[i].Files = append([]Receipt(nil), s.Units[i].Files...)
	}
	return n
}
func (s *Snapshot) Validate() error {
	if s.Revision == 0 || !s.Peer.ValidV3() || !protocol.Nonzero(s.Session[:]) || !protocol.Nonzero(s.Capability[:]) || s.Carrier != 1 || len(s.Units) > 8 {
		return protocol.Invalid
	}
	for i := range s.Units {
		u := &s.Units[i]
		r, e := protocol.Decode(u.Packet)
		if e != nil || r.Operation != protocol.Configure || r.Generation > u.Generation || r.Session != s.Session || r.Capability != s.Capability || u.Phase < Preparing || u.Phase > Removed || u.Generation == 0 || u.Index == 0 || u.Index > math.MaxUint16 || len(u.Files) != len(r.Resolvers) {
			return protocol.Invalid
		}
		u.Request = r
		if u.Generation == math.MaxUint64 && (u.KeepDNS || u.Phase == Preparing || u.Phase == Active) || (u.Phase == Preparing || u.Phase == Active || u.Phase == Removed) && u.KeepDNS || u.Phase == Retained && !u.KeepDNS || (u.Phase == Preparing || u.Phase == Active) && r.Generation != u.Generation {
			return protocol.Invalid
		}
		for _, f := range u.Files {
			if f.Phase > 2 || f.Phase == 0 && (f.Device != 0 || f.Inode != 0) || f.Phase != 0 && (f.Device == 0 || f.Inode == 0) || u.Phase == Removed && f.Phase != 0 || (u.Phase == Active || u.Phase == Retained || u.Phase == Removing && u.KeepDNS) && f.Phase != 2 {
				return protocol.Invalid
			}
		}
		for _, p := range s.Units[:i] {
			if r.Unit == p.Request.Unit || r.VPC == p.Request.VPC {
				return protocol.Invalid
			}
			if u.Phase == Removed || p.Phase == Removed {
				continue
			}
			for _, a := range r.Ranges {
				for _, b := range p.Request.Ranges {
					if a.Overlaps(b) {
						return protocol.Invalid
					}
				}
			}
			for _, a := range r.Resolvers {
				for _, b := range p.Request.Resolvers {
					if protocol.DNSOverlap(a.Domain, b.Domain) {
						return protocol.Invalid
					}
				}
			}
		}
	}
	return nil
}
func EncodeSnapshot(s Snapshot) ([]byte, error) {
	if e := s.Validate(); e != nil {
		return nil, e
	}
	var out bytes.Buffer
	out.WriteString("STLVSNP3")
	put := func(v any) { binary.Write(&out, binary.BigEndian, v) }
	put(s.Revision)
	put(s.Peer.UID)
	put(s.Peer.PID)
	put(s.Peer.Seconds)
	put(s.Peer.Micros)
	put(s.Peer.Token)
	put(s.Session)
	put(s.Capability)
	put(s.Carrier)
	put(byte(len(s.Units)))
	for _, u := range s.Units {
		put(u.Phase)
		var keep byte
		if u.KeepDNS {
			keep = 1
		}
		put(keep)
		put(u.Generation)
		put(u.Index)
		put(uint32(len(u.Packet)))
		out.Write(u.Packet)
		for _, f := range u.Files {
			put(f.Phase)
			put(f.Device)
			put(f.Inode)
		}
	}
	if out.Len() > protocol.MaxJournal {
		return nil, protocol.Invalid
	}
	return out.Bytes(), nil
}
func DecodeSnapshot(data []byte) (Snapshot, error) {
	var s Snapshot
	if len(data) > protocol.MaxJournal || len(data) < 8 || string(data[:8]) != "STLVSNP3" {
		return s, protocol.Invalid
	}
	buf := bytes.NewReader(data[8:])
	read := func(v any) bool { return binary.Read(buf, binary.BigEndian, v) == nil }
	var n byte
	if !read(&s.Revision) || !read(&s.Peer.UID) || !read(&s.Peer.PID) || !read(&s.Peer.Seconds) || !read(&s.Peer.Micros) || !read(&s.Peer.Token) || !read(&s.Session) || !read(&s.Capability) || !read(&s.Carrier) || !read(&n) || n > 8 {
		return Snapshot{}, protocol.Invalid
	}
	for i := byte(0); i < n; i++ {
		var u Unit
		var keep byte
		var length uint32
		if !read(&u.Phase) || !read(&keep) || keep > 1 || !read(&u.Generation) || !read(&u.Index) || !read(&length) || length > protocol.Header+protocol.MaxBody || int(length) > buf.Len() {
			return Snapshot{}, protocol.Invalid
		}
		u.KeepDNS = keep == 1
		u.Packet = make([]byte, length)
		buf.Read(u.Packet)
		r, e := protocol.Decode(u.Packet)
		if e != nil {
			return Snapshot{}, e
		}
		u.Request = r
		for range r.Resolvers {
			var f Receipt
			if !read(&f.Phase) || !read(&f.Device) || !read(&f.Inode) {
				return Snapshot{}, protocol.Invalid
			}
			u.Files = append(u.Files, f)
		}
		s.Units = append(s.Units, u)
	}
	if buf.Len() != 0 {
		return Snapshot{}, protocol.Invalid
	}
	if e := s.Validate(); e != nil {
		return Snapshot{}, e
	}
	return s, nil
}
func sameUnit(a, b Unit) bool {
	return a.Phase == b.Phase && a.KeepDNS == b.KeepDNS && a.Generation == b.Generation && a.Index == b.Index && bytes.Equal(a.Packet, b.Packet) && sameFiles(a.Files, b.Files)
}
func sameFiles(a, b []Receipt) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
func emptyFiles(files []Receipt) bool {
	for _, f := range files {
		if f.Phase != 0 {
			return false
		}
	}
	return true
}
func transition(a, b Unit) bool {
	if a.Request.Unit != b.Request.Unit || a.Request.VPC != b.Request.VPC {
		return false
	}
	if a.Phase == Removed {
		return b.Phase == Preparing && b.Generation > a.Generation && emptyFiles(b.Files)
	}
	if a.Index != b.Index {
		return false
	}
	if a.Phase == Active && b.Phase == Preparing && a.Generation == b.Generation && !a.KeepDNS && !b.KeepDNS && protocol.Extension(a.Request, b.Request) {
		return sameFiles(a.Files, b.Files[:len(a.Files)]) && emptyFiles(b.Files[len(a.Files):])
	}
	if !bytes.Equal(a.Packet, b.Packet) {
		return false
	}
	if b.Phase == Removing && (a.Phase != Removing || a.KeepDNS && !b.KeepDNS && b.Generation > a.Generation) {
		return b.Generation > a.Generation && sameFiles(a.Files, b.Files)
	}
	if a.Generation != b.Generation || a.KeepDNS != b.KeepDNS {
		return false
	}
	if a.Phase == Preparing && b.Phase == Active || a.Phase == Removing && (b.Phase == Removed || b.Phase == Retained) {
		return sameFiles(a.Files, b.Files)
	}
	if a.Phase != b.Phase || (a.Phase != Preparing && a.Phase != Removing) {
		return false
	}
	changes := 0
	for i, x := range a.Files {
		y := b.Files[i]
		if x == y {
			continue
		}
		changes++
		if changes > 1 {
			return false
		}
		if a.Phase == Preparing {
			if x.Phase == 0 && y.Phase == 1 || x.Phase == 1 && y.Phase == 2 && x.Device == y.Device && x.Inode == y.Inode {
				continue
			}
			return false
		}
		if a.KeepDNS || x.Phase == 0 || y.Phase != 0 {
			return false
		}
	}
	return changes == 1
}
func Successor(a, b Snapshot) bool {
	if a.Validate() != nil || b.Validate() != nil || a.Revision == math.MaxUint64 || b.Revision != a.Revision+1 || a.Peer != b.Peer || a.Session != b.Session || a.Capability != b.Capability || a.Carrier != b.Carrier || len(b.Units) < len(a.Units) || len(b.Units) > len(a.Units)+1 {
		return false
	}
	changes := 0
	for i, x := range a.Units {
		y := b.Units[i]
		if sameUnit(x, y) {
			continue
		}
		changes++
		if changes > 1 || !transition(x, y) {
			return false
		}
	}
	if len(b.Units) > len(a.Units) {
		u := b.Units[len(a.Units)]
		if changes != 0 || u.Phase != Preparing || !emptyFiles(u.Files) {
			return false
		}
		changes++
	}
	return changes == 1
}
