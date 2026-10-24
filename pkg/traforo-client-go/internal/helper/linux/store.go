//go:build linux

package helper

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/netip"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/stelviodev/traforo/internal/protocol"
	"golang.org/x/sys/unix"
)

const Installed = "/usr/local/libexec/stelvio-traforo"
const State = "/var/lib/stelvio/tunnel"
const Runtime = "/run/stelvio/tunnel"
const Socket = Runtime + "/helper.sock"
const journal = State + "/journal.json"
const servicePath = "/etc/systemd/system/stelvio-traforo.service"

type Unit struct {
	Request  protocol.Request `json:"request"`
	Slot     int              `json:"slot"`
	Phase    byte             `json:"phase"`
	TUN      string           `json:"tun"`
	DNS      string           `json:"dns"`
	Alias    string           `json:"alias"`
	TUNIndex int              `json:"tun_index"`
	DNSIndex int              `json:"dns_index"`
	Local    uint32           `json:"local"`
}
type Snapshot struct {
	Version    int      `json:"version"`
	Revision   uint64   `json:"revision"`
	Peer       Owner    `json:"peer"`
	Parent     Owner    `json:"parent"`
	Session    [16]byte `json:"session"`
	Capability [16]byte `json:"capability"`
	Units      []Unit   `json:"units"`
}

func noACL(path string) error {
	for _, name := range []string{"system.posix_acl_access", "system.posix_acl_default"} {
		_, err := unix.Lgetxattr(path, name, nil)
		if err == nil {
			return errUncertain
		}
		if err != unix.ENODATA && err != unix.ENOTSUP {
			return err
		}
	}
	return nil
}

// Ancestors are root-owned and not writable by clients, so checked child names
// cannot be swapped by an unprivileged actor between validation and open.
func securePath(path string, directory bool) error {
	if !filepath.IsAbs(path) || filepath.Clean(path) != path {
		return errUncertain
	}
	current := ""
	parts := strings.Split(strings.TrimPrefix(path, "/"), "/")
	for i, part := range parts {
		current += "/" + part
		var st unix.Stat_t
		if e := unix.Lstat(current, &st); e != nil {
			return e
		}
		if st.Uid != 0 || st.Mode&0022 != 0 || st.Mode&unix.S_IFMT == unix.S_IFLNK {
			return errUncertain
		}
		if i < len(parts)-1 || directory {
			if st.Mode&unix.S_IFMT != unix.S_IFDIR {
				return errUncertain
			}
		} else if st.Mode&unix.S_IFMT != unix.S_IFREG || st.Nlink != 1 {
			return errUncertain
		}
		if e := noACL(current); e != nil {
			return e
		}
	}
	return nil
}
func directories() error {
	for _, path := range []string{"/usr/local/libexec", "/var/lib/stelvio", State, "/run/stelvio", Runtime} {
		if e := securePath(filepath.Dir(path), true); e != nil {
			return e
		}
		if e := os.Mkdir(path, 0755); e != nil && !errors.Is(e, os.ErrExist) {
			return e
		}
		if e := securePath(path, true); e != nil {
			return e
		}
	}
	return nil
}
func exclusive(path string) (int, error) {
	fd, e := unix.Open(path, unix.O_CREAT|unix.O_RDWR|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0600)
	if e != nil {
		return -1, e
	}
	if e = securePath(path, false); e == nil {
		e = unix.Flock(fd, unix.LOCK_EX|unix.LOCK_NB)
	}
	if e != nil {
		_ = unix.Close(fd)
		return -1, e
	}
	return fd, nil
}

var vpcID = regexp.MustCompile(`^vpc-([0-9a-f]{8}|[0-9a-f]{17})$`)

func validateSnapshot(s Snapshot) error {
	if s.Version != 1 || s.Revision == 0 || s.Peer.UID == 0 || s.Peer.PID <= 1 || s.Peer.Start == 0 || len(s.Peer.Boot) != 36 || !protocol.Nonzero(s.Session[:]) || !protocol.Nonzero(s.Capability[:]) || len(s.Units) > protocol.MaxUnits {
		return errUncertain
	}
	if s.Parent.UID != s.Peer.UID || s.Parent.PID <= 1 || s.Parent.Start == 0 || s.Parent.Boot != s.Peer.Boot {
		return errUncertain
	}
	seen := map[uint32]bool{}
	slots := map[int]bool{}
	for _, u := range s.Units {
		if u.Slot < 0 || u.Slot >= 8 || slots[u.Slot] || u.Request.Unit == 0 || seen[u.Request.Unit] || u.Request.Generation == 0 || u.Request.Operation != protocol.Configure || u.Request.Session != s.Session || u.Request.Capability != s.Capability || u.Phase < 1 || u.Phase > 5 {
			return errUncertain
		}
		r := u.Request
		if !vpcID.MatchString(r.VPC) || len(r.Ranges) == 0 || len(r.Ranges) > protocol.MaxRanges || len(r.Resolvers) > protocol.MaxDomains {
			return errUncertain
		}
		for i, p := range r.Ranges {
			if _, e := protocol.Prefix(p.String()); e != nil {
				return errUncertain
			}
			for _, q := range r.Ranges[:i] {
				if p.Overlaps(q) {
					return errUncertain
				}
			}
		}
		for i, v := range r.Resolvers {
			name, e := protocol.Domain(v.Domain)
			if e != nil || name != v.Domain || v.Port < 1024 {
				return errUncertain
			}
			if _, e = netip.ParseAddr(name); e == nil {
				return errUncertain
			}
			for _, old := range r.Resolvers[:i] {
				if old.Domain == name {
					return errUncertain
				}
			}
		}
		tun, dns, alias := names(s, u.Request.Unit)
		if u.TUN != tun || u.DNS != dns || u.Alias != alias || u.Local != uint32(0xc0000201+2*u.Slot) || u.TUNIndex < 0 || u.DNSIndex < 0 {
			return errUncertain
		}
		seen[u.Request.Unit] = true
		slots[u.Slot] = true
	}
	return nil
}
func load() (Snapshot, bool, error) {
	var s Snapshot
	if e := securePath(journal, false); errors.Is(e, os.ErrNotExist) {
		return s, false, nil
	} else if e != nil {
		return s, true, e
	}
	f, e := os.Open(journal)
	if e != nil {
		return s, true, e
	}
	defer func() { _ = f.Close() }()
	data, e := io.ReadAll(io.LimitReader(f, protocol.MaxJournal+1))
	if e != nil || len(data) > protocol.MaxJournal {
		return s, true, errUncertain
	}
	return decodeSnapshot(data, true)
}

func decodeSnapshot(data []byte, exists bool) (Snapshot, bool, error) {
	var s Snapshot
	if len(data) == 0 || len(data) > protocol.MaxJournal {
		return s, exists, errUncertain
	}
	if e := uniqueJSON(data); e != nil {
		return s, exists, e
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if e := d.Decode(&s); e != nil {
		return s, true, e
	}
	if d.Decode(new(any)) != io.EOF {
		return s, true, errUncertain
	}
	return s, true, validateSnapshot(s)
}
func save(s *Snapshot) error {
	s.Version = 1
	s.Revision++
	if e := validateSnapshot(*s); e != nil {
		return e
	}
	data, e := json.Marshal(s)
	if e != nil || len(data) > protocol.MaxJournal {
		return errUncertain
	}
	return atomicFile(journal, data, 0600)
}
func atomicFile(path string, data []byte, mode os.FileMode) error {
	if e := securePath(filepath.Dir(path), true); e != nil {
		return e
	}
	if _, e := os.Lstat(path); e == nil {
		if e = securePath(path, false); e != nil {
			return e
		}
	} else if !errors.Is(e, os.ErrNotExist) {
		return e
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".traforo-")
	if e != nil {
		return e
	}
	name := f.Name()
	defer func() { _ = os.Remove(name) }()
	if e = f.Chmod(mode); e == nil {
		_, e = f.Write(data)
	}
	if e == nil {
		e = f.Sync()
	}
	e = errors.Join(e, f.Close())
	if e != nil {
		return e
	}
	if e = os.Rename(name, path); e != nil {
		return e
	}
	return syncDirectory(filepath.Dir(path))
}
func syncDirectory(path string) error {
	f, e := os.Open(path)
	if e != nil {
		return e
	}
	defer func() { _ = f.Close() }()
	return f.Sync()
}
func clearJournal() error {
	if e := securePath(journal, false); errors.Is(e, os.ErrNotExist) {
		return nil
	} else if e != nil {
		return e
	}
	if e := os.Remove(journal); e != nil {
		return e
	}
	return syncDirectory(State)
}

func sharedAdmission() (int, error) {
	path := State + "/admission.lock"
	fd, e := unix.Open(path, unix.O_CREAT|unix.O_RDWR|unix.O_CLOEXEC|unix.O_NOFOLLOW, 0600)
	if e != nil {
		return -1, e
	}
	if e = securePath(path, false); e == nil {
		e = unix.Flock(fd, unix.LOCK_SH|unix.LOCK_NB)
	}
	if e != nil {
		_ = unix.Close(fd)
		return -1, e
	}
	return fd, nil
}

func uniqueJSON(data []byte) error {
	d := json.NewDecoder(bytes.NewReader(data))
	var value func(int) error
	value = func(depth int) error {
		if depth > 16 {
			return errUncertain
		}
		token, e := d.Token()
		if e != nil {
			return e
		}
		delim, ok := token.(json.Delim)
		if !ok {
			return nil
		}
		switch delim {
		case '{':
			seen := map[string]bool{}
			for d.More() {
				key, e := d.Token()
				if e != nil {
					return e
				}
				name, ok := key.(string)
				if !ok || seen[name] {
					return errUncertain
				}
				seen[name] = true
				if e = value(depth + 1); e != nil {
					return e
				}
			}
		case '[':
			for d.More() {
				if e = value(depth + 1); e != nil {
					return e
				}
			}
		default:
			return errUncertain
		}
		end, e := d.Token()
		if e != nil {
			return e
		}
		if end != json.Delim('}') && end != json.Delim(']') {
			return errUncertain
		}
		return nil
	}
	if e := value(0); e != nil {
		return e
	}
	if _, e := d.Token(); e != io.EOF {
		return errUncertain
	}
	return nil
}
