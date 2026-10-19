//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"io"
	"math"
	"net/netip"

	"github.com/stelviodev/traforo/internal/protocol"
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
type ResolverHistory struct {
	Packet []byte
	Files  []Receipt
}
type Unit struct {
	Phase      byte
	KeepDNS    bool
	Generation uint64
	Index      uint32
	Packet     []byte
	Request    protocol.Request
	Files      []Receipt
	Previous   *ResolverHistory
}
type Snapshot struct {
	Revision            uint64
	Peer                protocol.DarwinOwner
	Parent              protocol.DarwinParent
	Session, Capability [16]byte
	Carrier             uint32
	Units               []Unit
}

func (s Snapshot) Clone() Snapshot {
	n := s
	n.Units = append([]Unit(nil), s.Units...)
	for i := range n.Units {
		n.Units[i].Files = append([]Receipt(nil), s.Units[i].Files...)
		if previous := s.Units[i].Previous; previous != nil {
			n.Units[i].Previous = &ResolverHistory{
				Packet: append([]byte(nil), previous.Packet...),
				Files:  append([]Receipt(nil), previous.Files...),
			}
		}
	}
	return n
}

func sameRanges(a, b []netip.Prefix) bool {
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

func preservesResolvers(previous, current protocol.Request) bool {
	for _, resolver := range previous.Resolvers {
		found := false
		for _, candidate := range current.Resolvers {
			if candidate == resolver {
				found = true
				break
			}
		}
		if !found {
			return false
		}
	}
	return true
}

func (s *Snapshot) Validate() error {
	if s.Parent != (protocol.DarwinParent{}) && (!s.Parent.Valid() || s.Parent.UID != s.Peer.UID) {
		return protocol.ErrInvalid
	}
	if s.Revision == 0 || !s.Peer.ValidV3() || !protocol.Nonzero(s.Session[:]) || !protocol.Nonzero(s.Capability[:]) || s.Carrier != 1 || len(s.Units) > 8 {
		return protocol.ErrInvalid
	}
	for i := range s.Units {
		u := &s.Units[i]
		r, e := protocol.Decode(u.Packet)
		if e != nil || r.Operation != protocol.Configure || r.Generation > u.Generation || r.Session != s.Session || r.Capability != s.Capability || u.Phase < Preparing || u.Phase > Removed || u.Generation == 0 || u.Index == 0 || u.Index > math.MaxUint16 || len(u.Files) != len(r.Resolvers) {
			return protocol.ErrInvalid
		}
		u.Request = r
		if u.Generation == math.MaxUint64 && (u.KeepDNS || u.Phase == Preparing || u.Phase == Active) || (u.Phase == Preparing || u.Phase == Active || u.Phase == Removed) && u.KeepDNS || u.Phase == Retained && !u.KeepDNS || (u.Phase == Preparing || u.Phase == Active) && r.Generation != u.Generation {
			return protocol.ErrInvalid
		}
		for _, f := range u.Files {
			if f.Phase > 2 || f.Phase == 0 && (f.Device != 0 || f.Inode != 0) || f.Phase != 0 && (f.Device == 0 || f.Inode == 0) || u.Phase == Removed && f.Phase != 0 || (u.Phase == Active || u.Phase == Retained || u.Phase == Removing && u.KeepDNS) && f.Phase != 2 {
				return protocol.ErrInvalid
			}
		}
		if previous := u.Previous; previous != nil {
			old, err := protocol.Decode(previous.Packet)
			if err != nil || old.Operation != protocol.Configure || old.Generation >= r.Generation || old.Session != r.Session || old.Capability != r.Capability || old.Unit != r.Unit || old.VPC != r.VPC || !sameRanges(old.Ranges, r.Ranges) || !preservesResolvers(old, r) || len(previous.Files) != len(old.Resolvers) || (u.Phase != Preparing && u.Phase != Active && u.Phase != Removing) {
				return protocol.ErrInvalid
			}
			for _, f := range previous.Files {
				if f.Phase != 0 && f.Phase != 2 || f.Phase == 0 && (f.Device != 0 || f.Inode != 0) || f.Phase == 2 && (f.Device == 0 || f.Inode == 0) {
					return protocol.ErrInvalid
				}
				if u.Phase == Preparing && f.Phase != 2 {
					return protocol.ErrInvalid
				}
			}
		}
		for _, p := range s.Units[:i] {
			if r.Unit == p.Request.Unit || r.VPC == p.Request.VPC {
				return protocol.ErrInvalid
			}
			if u.Phase == Removed || p.Phase == Removed {
				continue
			}
			for _, a := range r.Ranges {
				for _, b := range p.Request.Ranges {
					if a.Overlaps(b) {
						return protocol.ErrInvalid
					}
				}
			}
			for _, a := range r.Resolvers {
				for _, b := range p.Request.Resolvers {
					if protocol.DNSOverlap(a.Domain, b.Domain) {
						return protocol.ErrInvalid
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
	var writeErr error
	hasPrevious := false
	for _, u := range s.Units {
		hasPrevious = hasPrevious || u.Previous != nil
	}
	if hasPrevious {
		out.WriteString("STLVSNP5")
	} else if s.Parent.PID != 0 {
		out.WriteString("STLVSNP4")
	} else {
		out.WriteString("STLVSNP3")
	}
	put := func(v any) {
		if writeErr == nil {
			writeErr = binary.Write(&out, binary.BigEndian, v)
		}
	}
	put(s.Revision)
	put(s.Peer.UID)
	put(s.Peer.PID)
	put(s.Peer.Seconds)
	put(s.Peer.Micros)
	put(s.Peer.Token)
	if hasPrevious || s.Parent.PID != 0 {
		put(s.Parent)
	}
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
		if hasPrevious {
			if u.Previous == nil {
				put(byte(0))
				continue
			}
			put(byte(1))
			put(uint32(len(u.Previous.Packet)))
			out.Write(u.Previous.Packet)
			for _, f := range u.Previous.Files {
				put(f.Phase)
				put(f.Device)
				put(f.Inode)
			}
		}
	}
	if writeErr != nil {
		return nil, writeErr
	}
	if out.Len() > protocol.MaxJournal {
		return nil, protocol.ErrInvalid
	}
	return out.Bytes(), nil
}
func DecodeSnapshot(data []byte) (Snapshot, error) {
	var s Snapshot
	version := ""
	if len(data) >= 8 {
		version = string(data[:8])
	}
	if len(data) > protocol.MaxJournal || len(data) < 8 || (version != "STLVSNP3" && version != "STLVSNP4" && version != "STLVSNP5") {
		return s, protocol.ErrInvalid
	}
	buf := bytes.NewReader(data[8:])
	read := func(v any) bool { return binary.Read(buf, binary.BigEndian, v) == nil }
	var n byte
	if !read(&s.Revision) || !read(&s.Peer.UID) || !read(&s.Peer.PID) || !read(&s.Peer.Seconds) || !read(&s.Peer.Micros) || !read(&s.Peer.Token) || (version == "STLVSNP4" && (!read(&s.Parent) || !s.Parent.Valid())) || (version == "STLVSNP5" && (!read(&s.Parent) || s.Parent.PID != 0 && !s.Parent.Valid())) || !read(&s.Session) || !read(&s.Capability) || !read(&s.Carrier) || !read(&n) || n > 8 {
		return Snapshot{}, protocol.ErrInvalid
	}
	for i := byte(0); i < n; i++ {
		var u Unit
		var keep byte
		var length uint32
		if !read(&u.Phase) || !read(&keep) || keep > 1 || !read(&u.Generation) || !read(&u.Index) || !read(&length) || length > protocol.Header+protocol.MaxBody || int(length) > buf.Len() {
			return Snapshot{}, protocol.ErrInvalid
		}
		u.KeepDNS = keep == 1
		u.Packet = make([]byte, length)
		if _, e := io.ReadFull(buf, u.Packet); e != nil {
			return Snapshot{}, protocol.ErrInvalid
		}
		r, e := protocol.Decode(u.Packet)
		if e != nil {
			return Snapshot{}, e
		}
		u.Request = r
		for range r.Resolvers {
			var f Receipt
			if !read(&f.Phase) || !read(&f.Device) || !read(&f.Inode) {
				return Snapshot{}, protocol.ErrInvalid
			}
			u.Files = append(u.Files, f)
		}
		if version == "STLVSNP5" {
			var present byte
			if !read(&present) || present > 1 {
				return Snapshot{}, protocol.ErrInvalid
			}
			if present == 1 {
				var previousLength uint32
				if !read(&previousLength) || previousLength == 0 || previousLength > protocol.Header+protocol.MaxBody || int(previousLength) > buf.Len() {
					return Snapshot{}, protocol.ErrInvalid
				}
				previous := &ResolverHistory{Packet: make([]byte, previousLength)}
				if _, e := io.ReadFull(buf, previous.Packet); e != nil {
					return Snapshot{}, protocol.ErrInvalid
				}
				old, e := protocol.Decode(previous.Packet)
				if e != nil {
					return Snapshot{}, e
				}
				for range old.Resolvers {
					var f Receipt
					if !read(&f.Phase) || !read(&f.Device) || !read(&f.Inode) {
						return Snapshot{}, protocol.ErrInvalid
					}
					previous.Files = append(previous.Files, f)
				}
				u.Previous = previous
			}
		}
		s.Units = append(s.Units, u)
	}
	if buf.Len() != 0 {
		return Snapshot{}, protocol.ErrInvalid
	}
	if e := s.Validate(); e != nil {
		return Snapshot{}, e
	}
	return s, nil
}
func samePrevious(a, b *ResolverHistory) bool {
	if a == nil || b == nil {
		return a == b
	}
	return bytes.Equal(a.Packet, b.Packet) && sameFiles(a.Files, b.Files)
}

func sameUnit(a, b Unit) bool {
	return a.Phase == b.Phase && a.KeepDNS == b.KeepDNS && a.Generation == b.Generation && a.Index == b.Index && bytes.Equal(a.Packet, b.Packet) && sameFiles(a.Files, b.Files) && samePrevious(a.Previous, b.Previous)
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
	if a.Phase == Retained && b.Phase == Preparing {
		old, err := protocol.Decode(a.Packet)
		if err != nil || b.Previous == nil || !bytes.Equal(b.Previous.Packet, a.Packet) || !sameFiles(b.Previous.Files, a.Files) {
			return false
		}
		current, err := protocol.Decode(b.Packet)
		return err == nil && a.KeepDNS && !b.KeepDNS && b.Generation > a.Generation && b.Generation == current.Generation && old.Unit == current.Unit && old.VPC == current.VPC && sameRanges(old.Ranges, current.Ranges) && preservesResolvers(old, current) && emptyFiles(b.Files)
	}
	if a.Index != b.Index {
		return false
	}
	if a.Phase == Active && b.Phase == Preparing && a.Generation == b.Generation && !a.KeepDNS && !b.KeepDNS && protocol.Extension(a.Request, b.Request) {
		return sameFiles(a.Files, b.Files[:len(a.Files)]) && emptyFiles(b.Files[len(a.Files):]) && samePrevious(a.Previous, b.Previous)
	}
	if !bytes.Equal(a.Packet, b.Packet) {
		return false
	}
	if b.Phase == Removing && (a.Phase != Removing || a.KeepDNS && !b.KeepDNS && b.Generation > a.Generation) {
		return b.Generation > a.Generation && sameFiles(a.Files, b.Files) && samePrevious(a.Previous, b.Previous)
	}
	if a.Generation != b.Generation || a.KeepDNS != b.KeepDNS {
		return false
	}
	if a.Phase == Preparing && b.Phase == Active {
		return sameFiles(a.Files, b.Files) && samePrevious(a.Previous, b.Previous)
	}
	if a.Phase == Removing && (b.Phase == Removed || b.Phase == Retained) {
		return sameFiles(a.Files, b.Files) && a.Previous == nil && b.Previous == nil
	}
	if (a.Phase == Active && b.Phase == Active || a.Phase == Removing && b.Phase == Removing) && bytes.Equal(a.Packet, b.Packet) && sameFiles(a.Files, b.Files) {
		if a.Previous != nil && b.Previous == nil {
			return emptyFiles(a.Previous.Files)
		}
		if a.Previous != nil && b.Previous != nil && bytes.Equal(a.Previous.Packet, b.Previous.Packet) {
			return onePreviousFileRetired(a.Previous.Files, b.Previous.Files)
		}
		return false
	}
	if a.Phase != b.Phase || (a.Phase != Preparing && a.Phase != Removing) || !samePrevious(a.Previous, b.Previous) {
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

func onePreviousFileRetired(a, b []Receipt) bool {
	if len(a) != len(b) {
		return false
	}
	changes := 0
	for i := range a {
		if a[i] == b[i] {
			continue
		}
		if a[i].Phase != 2 || b[i] != (Receipt{}) {
			return false
		}
		changes++
	}
	return changes == 1
}
func Successor(a, b Snapshot) bool {
	if a.Validate() != nil || b.Validate() != nil || a.Revision == math.MaxUint64 || b.Revision != a.Revision+1 || a.Peer != b.Peer || a.Parent != b.Parent || a.Session != b.Session || a.Capability != b.Capability || a.Carrier != b.Carrier || len(b.Units) < len(a.Units) || len(b.Units) > len(a.Units)+1 {
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
