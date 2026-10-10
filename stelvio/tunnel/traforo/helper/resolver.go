//go:build darwin

package helper

import (
	"bytes"
	"encoding/hex"
	"fmt"
	"golang.org/x/sys/unix"
	"os"
	"stelvio.dev/traforo/darwin"
	"stelvio.dev/traforo/protocol"
	"strings"
)

const resolverDirectory = "/etc/resolver"

type resolverSpec struct {
	private, public string
	contents        []byte
}

func spec(s Snapshot, unit, domain int) resolverSpec {
	r := s.Units[unit].Request
	session := hex.EncodeToString(s.Session[:])
	capability := hex.EncodeToString(s.Capability[:])
	endpoint := r.Resolvers[domain]
	return resolverSpec{
		fmt.Sprintf("pending.%s.%s.%08x.%016x.%02x", capability, session, r.Unit, r.Generation, domain),
		fmt.Sprintf("stelvio.%s.%08x.%016x.%02x", session, r.Unit, r.Generation, domain),
		[]byte(fmt.Sprintf("# Stelvio tunnel/1 session=%s unit=%08x generation=%d\ndomain %s\nnameserver 127.0.0.1\nport %d\nsearch_order 0\n", session, r.Unit, r.Generation, endpoint.Domain, endpoint.Port)),
	}
}
func owned(parent int, name string, receipt Receipt, expected []byte, prefix bool) (bool, error) {
	data, st, e := readAt(parent, name, len(expected))
	if e == unix.ENOENT {
		return false, nil
	}
	if e != nil {
		return false, e
	}
	if st.Mode&0777 != 0644 || (st.Nlink != 1 && st.Nlink != 2) || uint64(st.Dev) != receipt.Device || st.Ino != receipt.Inode || !prefix && len(data) != len(expected) || !bytes.Equal(data, expected[:len(data)]) {
		return false, uncertain
	}
	return true, nil
}
func saveReceipt(store Store, state *Snapshot, i, j int, r Receipt) error {
	next := state.Clone()
	next.Revision++
	next.Units[i].Files[j] = r
	if e := store.Save(next); e != nil {
		return e
	}
	*state = next
	return nil
}
func stageResolver(store Store, state *Snapshot, i, j int) error {
	if !store.Current(*state) || state.Units[i].Phase != Preparing || state.Units[i].Files[j].Phase != 0 {
		return uncertain
	}
	p, e := safeDirectory(darwin.State)
	if e != nil {
		return e
	}
	defer unix.Close(p)
	s := spec(*state, i, j)
	fd, st, e := prefixAt(p, s.private, s.contents, 0644)
	if e != nil {
		return e
	}
	e = writeAll(fd, s.contents[int(st.Size):])
	if e == nil {
		e = unix.Fsync(fd)
	}
	if e == nil {
		e = unix.Fsync(p)
	}
	if e == nil && !namedCurrent(p, s.private, st) {
		e = uncertain
	}
	c := unix.Close(fd)
	if e == nil {
		e = c
	}
	if e != nil {
		return e
	}
	return saveReceipt(store, state, i, j, Receipt{1, uint64(st.Dev), st.Ino})
}
func publishResolver(store Store, state *Snapshot, i, j int) error {
	if !store.Current(*state) || state.Units[i].Phase != Preparing || state.Units[i].Files[j].Phase != 1 {
		return uncertain
	}
	p, e := safeDirectory(darwin.State)
	if e != nil {
		return e
	}
	defer unix.Close(p)
	q, e := safeDirectory(resolverDirectory)
	if e != nil {
		return e
	}
	defer unix.Close(q)
	s := spec(*state, i, j)
	r := state.Units[i].Files[j]
	staged, e := owned(p, s.private, r, s.contents, false)
	if e != nil {
		return e
	}
	linked, e := owned(q, s.public, r, s.contents, false)
	if e != nil || !staged && !linked {
		return uncertain
	}
	if !linked {
		if e = unix.Linkat(p, s.private, q, s.public, 0); e != nil {
			return e
		}
	}
	ok, e := owned(q, s.public, r, s.contents, false)
	if e != nil || !ok {
		return uncertain
	}
	if e = unix.Fsync(q); e != nil {
		return e
	}
	if staged {
		if e = unix.Unlinkat(p, s.private, 0); e != nil {
			return e
		}
	}
	if e = unix.Fsync(p); e != nil {
		return e
	}
	r.Phase = 2
	return saveReceipt(store, state, i, j, r)
}
func resolverCurrent(store Store, state Snapshot, i, j int) bool {
	if !store.Current(state) || state.Units[i].Files[j].Phase != 2 {
		return false
	}
	p, e := safeDirectory(resolverDirectory)
	if e != nil {
		return false
	}
	defer unix.Close(p)
	s := spec(state, i, j)
	ok, e := owned(p, s.public, state.Units[i].Files[j], s.contents, false)
	return ok && e == nil
}
func unpublish(p, q int, s resolverSpec, r Receipt) error {
	retired := s.private + ".retired"
	if r.Phase == 0 {
		var st unix.Stat_t
		if unix.Fstatat(p, retired, &st, unix.AT_SYMLINK_NOFOLLOW) != unix.ENOENT {
			return uncertain
		}
		found, e := owned(q, s.public, r, s.contents, false)
		if e != nil || found {
			return uncertain
		}
		return nil
	}
	moved, e := owned(p, retired, r, s.contents, false)
	if e != nil {
		darwin.RenameExclusive(p, retired, q, s.public)
		unix.Fsync(p)
		unix.Fsync(q)
		return uncertain
	}
	if !moved {
		linked, e := owned(q, s.public, r, s.contents, false)
		if e != nil {
			return e
		}
		if !linked {
			return nil
		}
		if e = darwin.RenameExclusive(q, s.public, p, retired); e != nil {
			return e
		}
		moved, e = owned(p, retired, r, s.contents, false)
		if e != nil || !moved {
			darwin.RenameExclusive(p, retired, q, s.public)
			unix.Fsync(q)
			unix.Fsync(p)
			return uncertain
		}
	}
	if e = unix.Fsync(q); e != nil {
		return e
	}
	if e = unix.Fsync(p); e != nil {
		return e
	}
	if e = unix.Unlinkat(p, retired, 0); e != nil {
		return e
	}
	if e = unix.Fsync(p); e != nil {
		return e
	}
	found, e := owned(q, s.public, r, s.contents, false)
	if e != nil || found {
		return uncertain
	}
	return nil
}
func removeResolver(store Store, state *Snapshot, i, j int) error {
	if !store.Current(*state) || state.Units[i].Phase != Removing {
		return uncertain
	}
	p, e := safeDirectory(darwin.State)
	if e != nil {
		return e
	}
	defer unix.Close(p)
	q, e := safeDirectory(resolverDirectory)
	if e != nil {
		return e
	}
	defer unix.Close(q)
	s := spec(*state, i, j)
	r := state.Units[i].Files[j]
	staged, e := owned(p, s.private, r, s.contents, true)
	if e != nil || r.Phase == 0 && staged {
		return uncertain
	}
	if e = unpublish(p, q, s, r); e != nil {
		return e
	}
	if e = unix.Fsync(q); e != nil {
		return e
	}
	if staged {
		if e = unix.Unlinkat(p, s.private, 0); e != nil {
			return e
		}
	}
	if e = unix.Fsync(p); e != nil {
		return e
	}
	if r.Phase != 0 {
		return saveReceipt(store, state, i, j, Receipt{})
	}
	return nil
}
func resolverDomain(name string, data []byte) (string, error) {
	if len(data) > 8192 || bytes.IndexByte(data, 0) >= 0 {
		return "", uncertain
	}
	domain := ""
	found := false
	for _, line := range strings.Split(string(data), "\n") {
		fields := strings.Fields(strings.SplitN(line, "#", 2)[0])
		if len(fields) > 0 && fields[0] == "domain" {
			if found || len(fields) != 2 {
				return "", uncertain
			}
			found = true
			var e error
			domain, e = protocol.Domain(fields[1])
			if e != nil {
				return "", e
			}
		}
	}
	if !found {
		return protocol.Domain(name)
	}
	return domain, nil
}
func resolverConflict(store Store, state Snapshot, r protocol.Request) error {
	if !store.Current(state) {
		return uncertain
	}
	domains, e := darwin.DNSDomains()
	if e != nil {
		return e
	}
	overlap := func(value string) error {
		domain, e := protocol.Domain(value)
		if e != nil {
			return e
		}
		for _, endpoint := range r.Resolvers {
			if protocol.DNSOverlap(domain, endpoint.Domain) {
				return uncertain
			}
		}
		return nil
	}
	for _, d := range domains {
		if e = overlap(d); e != nil {
			return e
		}
	}
	p, e := safeDirectory(resolverDirectory)
	if e != nil {
		return e
	}
	file := os.NewFile(uintptr(p), resolverDirectory)
	defer file.Close()
	names, e := file.Readdirnames(4097)
	if e != nil && len(names) == 0 && e.Error() != "EOF" {
		return e
	}
	if len(names) > 4096 {
		return uncertain
	}
	for _, name := range names {
		ours := false
		for i, u := range state.Units {
			for j := range u.Files {
				s := spec(state, i, j)
				if s.public != name {
					continue
				}
				ok, e := owned(p, name, u.Files[j], s.contents, false)
				if e != nil || !ok || u.Files[j].Phase == 0 {
					return uncertain
				}
				ours = true
			}
		}
		if ours {
			continue
		}
		data, _, e := readAt(p, name, 8192)
		if e != nil {
			return e
		}
		d, e := resolverDomain(name, data)
		if e != nil {
			return e
		}
		if e = overlap(d); e != nil {
			return e
		}
	}
	return nil
}
