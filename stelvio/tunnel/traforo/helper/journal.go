//go:build darwin

package helper

import (
	"bytes"
	"encoding/binary"
	"golang.org/x/sys/unix"
	"hash/fnv"
	"stelvio.dev/traforo/darwin"
	"stelvio.dev/traforo/protocol"
)

type Store struct{ Image, Lease int }

func (s Store) directory() (int, error) {
	if !trusted() {
		return -1, uncertain
	}
	for _, v := range []struct {
		fd   int
		path string
		mask uint16
	}{{s.Image, darwin.Installed, 022}, {s.Lease, darwin.State + "/lease", 077}} {
		st, e := stat(v.fd)
		var n unix.Stat_t
		if e != nil || st.Uid != 0 || st.Nlink != 1 || st.Mode&unix.S_IFMT != unix.S_IFREG || st.Mode&v.mask != 0 || !darwin.NoACL(v.fd) || unix.Lstat(v.path, &n) != nil || !same(st, n) || unix.Flock(v.fd, unix.LOCK_EX|unix.LOCK_NB) != nil {
			return -1, uncertain
		}
	}
	return safeDirectory(darwin.State)
}
func checksum(data []byte) uint32 { h := fnv.New32a(); h.Write(data); return h.Sum32() }
func journalPayload(data []byte) ([]byte, error) {
	if len(data) < 16 || len(data) > protocol.MaxJournal+16 || string(data[:8]) != "STLVJNL1" {
		return nil, uncertain
	}
	n := int(binary.BigEndian.Uint32(data[8:]))
	if n == 0 || n != len(data)-16 || binary.BigEndian.Uint32(data[12:]) != checksum(data[16:]) {
		return nil, uncertain
	}
	return data[16:], nil
}
func incomplete(data []byte) bool {
	header := data
	if len(header) > 16 {
		header = header[:16]
	}
	return incompleteHeader(header, uint64(len(data)))
}
func incompleteHeader(header []byte, total uint64) bool {
	count := total
	if count > 16 {
		count = 16
	}
	if uint64(len(header)) != count || total > protocol.MaxJournal+16 {
		return false
	}
	n := len(header)
	if n > 8 {
		n = 8
	}
	if !bytes.Equal(header[:n], []byte("STLVJNL1")[:n]) {
		return false
	}
	var length [4]byte
	if len(header) > 8 {
		end := len(header)
		if end > 12 {
			end = 12
		}
		copy(length[:], header[8:end])
	}
	size := binary.BigEndian.Uint32(length[:])
	return size <= protocol.MaxJournal && (len(header) < 12 || size != 0 && total < 16+uint64(size))
}
func readJournal(parent int, name string) ([]byte, unix.Stat_t, error) {
	data, st, e := readAt(parent, name, protocol.MaxJournal+16)
	if e != nil {
		return nil, st, e
	}
	if st.Nlink != 1 || st.Mode&0777 != 0600 {
		return nil, st, uncertain
	}
	return data, st, nil
}
func accepted(old, next []byte) bool {
	var a Snapshot
	if old != nil {
		payload, e := journalPayload(old)
		if e != nil {
			return false
		}
		a, e = DecodeSnapshot(payload)
		if e != nil {
			return false
		}
	}
	if next == nil {
		return true
	}
	payload, e := journalPayload(next)
	if e != nil {
		return false
	}
	b, e := DecodeSnapshot(payload)
	if e != nil {
		return false
	}
	if old == nil {
		return b.Revision == 1 && len(b.Units) == 0
	}
	return Successor(a, b)
}
func (s Store) recover(parent int) error {
	old, _, e := readJournal(parent, "journal")
	if e != nil && e != unix.ENOENT {
		return e
	}
	if e == unix.ENOENT {
		old = nil
	}
	next, st, e := readJournal(parent, "journal.next")
	if e == unix.ENOENT {
		return unix.Fsync(parent)
	}
	if e != nil {
		return e
	}
	if !accepted(old, next) {
		if !accepted(old, nil) || !incomplete(next) {
			return uncertain
		}
		if !namedCurrent(parent, "journal.next", st) {
			return uncertain
		}
		if e = unix.Unlinkat(parent, "journal.next", 0); e != nil {
			return e
		}
		return unix.Fsync(parent)
	}
	fd, e := unix.Openat(parent, "journal.next", unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if e != nil {
		return e
	}
	opened, e := stat(fd)
	if e != nil || !same(st, opened) || !darwin.NoACL(fd) || !namedCurrent(parent, "journal.next", st) || unix.Fsync(fd) != nil {
		unix.Close(fd)
		return uncertain
	}
	if e = unix.Close(fd); e != nil {
		return e
	}
	if e = unix.Renameat(parent, "journal.next", parent, "journal"); e != nil {
		return e
	}
	return unix.Fsync(parent)
}
func (s Store) Load() (Snapshot, bool, error) {
	p, e := s.directory()
	if e != nil {
		return Snapshot{}, false, e
	}
	defer unix.Close(p)
	if e = s.recover(p); e != nil {
		return Snapshot{}, false, e
	}
	data, _, e := readJournal(p, "journal")
	if e == unix.ENOENT {
		return Snapshot{}, false, nil
	}
	if e != nil {
		return Snapshot{}, false, e
	}
	payload, e := journalPayload(data)
	if e != nil {
		return Snapshot{}, false, e
	}
	state, e := DecodeSnapshot(payload)
	return state, e == nil, e
}
func (s Store) Save(state Snapshot) error {
	payload, e := EncodeSnapshot(state)
	if e != nil {
		return e
	}
	p, e := s.directory()
	if e != nil {
		return e
	}
	defer unix.Close(p)
	if e = s.recover(p); e != nil {
		return e
	}
	old, st, e := readJournal(p, "journal")
	if e != nil && e != unix.ENOENT {
		return e
	}
	if e == unix.ENOENT {
		old = nil
	}
	data := make([]byte, 16+len(payload))
	copy(data, "STLVJNL1")
	binary.BigEndian.PutUint32(data[8:], uint32(len(payload)))
	binary.BigEndian.PutUint32(data[12:], checksum(payload))
	copy(data[16:], payload)
	if !accepted(old, data) {
		return uncertain
	}
	fd, e := unix.Openat(p, "journal.next", unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if e != nil {
		return e
	}
	created, e := stat(fd)
	if e != nil || !darwin.NoACL(fd) || created.Nlink != 1 || created.Uid != 0 || created.Mode&0777 != 0600 {
		unix.Close(fd)
		return uncertain
	}
	if e = writeAll(fd, data); e == nil {
		e = unix.Fsync(fd)
	}
	if e == nil && !namedCurrent(p, "journal.next", created) {
		e = uncertain
	}
	c := unix.Close(fd)
	if e == nil {
		e = c
	}
	if e != nil {
		return e
	}
	if old != nil && !namedCurrent(p, "journal", st) {
		return uncertain
	}
	if old == nil {
		var target unix.Stat_t
		if unix.Fstatat(p, "journal", &target, unix.AT_SYMLINK_NOFOLLOW) != unix.ENOENT {
			return uncertain
		}
	}
	if e = unix.Renameat(p, "journal.next", p, "journal"); e != nil {
		return e
	}
	return unix.Fsync(p)
}
func (s Store) Current(state Snapshot) bool {
	current, exists, e := s.Load()
	if e != nil || !exists {
		return false
	}
	a, e := EncodeSnapshot(state)
	if e != nil {
		return false
	}
	b, e := EncodeSnapshot(current)
	return e == nil && bytes.Equal(a, b)
}
func (s Store) Remove() error {
	p, e := s.directory()
	if e != nil {
		return e
	}
	defer unix.Close(p)
	var st unix.Stat_t
	if unix.Fstatat(p, "journal.next", &st, unix.AT_SYMLINK_NOFOLLOW) != unix.ENOENT {
		return uncertain
	}
	data, st, e := readJournal(p, "journal")
	if e == unix.ENOENT {
		return unix.Fsync(p)
	}
	if e != nil || !accepted(data, nil) || !namedCurrent(p, "journal", st) {
		return uncertain
	}
	if e = unix.Unlinkat(p, "journal", 0); e != nil {
		return e
	}
	return unix.Fsync(p)
}
