//go:build darwin

package helper

import (
	"bytes"
	"errors"
	"github.com/stelviodev/traforo/darwin"
	"golang.org/x/sys/unix"
	"io"
	"os"
	"path/filepath"
)

var uncertain = errors.New("ownership cannot be certified")

func same(a, b unix.Stat_t) bool       { return a.Dev == b.Dev && a.Ino == b.Ino }
func stat(fd int) (unix.Stat_t, error) { var st unix.Stat_t; e := unix.Fstat(fd, &st); return st, e }
func safeDirectory(path string) (int, error) {
	fd, e := unix.Open(path, unix.O_RDONLY|unix.O_DIRECTORY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if e != nil {
		return -1, e
	}
	st, e := stat(fd)
	var named unix.Stat_t
	if e != nil || unix.Lstat(path, &named) != nil || !same(st, named) || st.Uid != 0 || st.Mode&022 != 0 || !darwin.NoACL(fd) {
		unix.Close(fd)
		return -1, uncertain
	}
	return fd, nil
}
func syncDirectory(path string) error {
	fd, e := safeDirectory(path)
	if e != nil {
		return e
	}
	e = unix.Fsync(fd)
	c := unix.Close(fd)
	if e != nil {
		return e
	}
	return c
}
func noACLPath(path string) bool { return darwin.NoACLPath(path) }
func trusted() bool              { ok, _ := trustStage(); return ok }
func trustStage() (bool, int) {
	if !darwin.Root() {
		return false, 1
	}
	path, dev, ino, e := darwin.Image()
	if e != nil || path != darwin.Installed {
		return false, 2
	}
	var st unix.Stat_t
	if unix.Lstat(path, &st) != nil || st.Uid != 0 || st.Mode&unix.S_IFMT != unix.S_IFREG || st.Nlink != 1 || st.Mode&022 != 0 || uint64(uint32(st.Dev)) != dev || st.Ino != ino || !noACLPath(path) {
		return false, 3
	}
	for _, path := range []string{"/Library", "/Library/PrivilegedHelperTools"} {
		fd, e := safeDirectory(path)
		if e != nil {
			return false, 4
		}
		unix.Close(fd)
	}
	return true, 0
}
func lockFile(path string, private, shared, create bool) (int, error) {
	flags := unix.O_RDWR | unix.O_NOFOLLOW | unix.O_CLOEXEC | unix.O_NONBLOCK
	if path == darwin.Installed {
		flags = unix.O_RDONLY | unix.O_NOFOLLOW | unix.O_CLOEXEC | unix.O_NONBLOCK
	}
	if create {
		flags |= unix.O_CREAT
	}
	fd, e := unix.Open(path, flags, 0600)
	if e != nil {
		return -1, e
	}
	st, e := stat(fd)
	var named unix.Stat_t
	mask := uint16(022)
	if private {
		mask = 077
	}
	mode := unix.LOCK_EX | unix.LOCK_NB
	if shared {
		mode = unix.LOCK_SH | unix.LOCK_NB
	}
	if e != nil || st.Uid != 0 || st.Nlink != 1 || st.Mode&unix.S_IFMT != unix.S_IFREG || st.Mode&mask != 0 || !darwin.NoACL(fd) || unix.Flock(fd, mode) != nil || unix.Lstat(path, &named) != nil || !same(st, named) {
		unix.Close(fd)
		return -1, uncertain
	}
	return fd, nil
}
func namedCurrent(parent int, name string, st unix.Stat_t) bool {
	var other unix.Stat_t
	return unix.Fstatat(parent, name, &other, unix.AT_SYMLINK_NOFOLLOW) == nil && same(st, other)
}
func readAt(parent int, name string, max int) ([]byte, unix.Stat_t, error) {
	fd, e := unix.Openat(parent, name, unix.O_RDONLY|unix.O_NONBLOCK|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if e != nil {
		return nil, unix.Stat_t{}, e
	}
	st, e := stat(fd)
	if e != nil || st.Mode&unix.S_IFMT != unix.S_IFREG || st.Uid != 0 || st.Mode&022 != 0 || st.Size < 0 || st.Size > int64(max) || !darwin.NoACL(fd) {
		unix.Close(fd)
		return nil, st, uncertain
	}
	file := os.NewFile(uintptr(fd), name)
	data, e := io.ReadAll(io.LimitReader(file, int64(max)+1))
	if e == nil && (!namedCurrent(parent, name, st) || int64(len(data)) != st.Size) {
		e = uncertain
	}
	c := file.Close()
	if e == nil {
		e = c
	}
	return data, st, e
}
func writeAll(fd int, data []byte) error {
	for len(data) > 0 {
		n, e := unix.Write(fd, data)
		if e == unix.EINTR {
			continue
		}
		if e != nil {
			return e
		}
		if n <= 0 {
			return io.ErrShortWrite
		}
		data = data[n:]
	}
	return nil
}
func moveExclusive(source, target string) error {
	a, e := safeDirectory(filepath.Dir(source))
	if e != nil {
		return e
	}
	defer unix.Close(a)
	b, e := safeDirectory(filepath.Dir(target))
	if e != nil {
		return e
	}
	defer unix.Close(b)
	return darwin.RenameExclusive(a, filepath.Base(source), b, filepath.Base(target))
}
func prefixAt(parent int, name string, expected []byte, mode uint32) (int, unix.Stat_t, error) {
	fd, e := unix.Openat(parent, name, unix.O_RDWR|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, mode)
	if e == unix.EEXIST {
		fd, e = unix.Openat(parent, name, unix.O_RDWR|unix.O_NONBLOCK|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	}
	if e != nil {
		return -1, unix.Stat_t{}, e
	}
	st, e := stat(fd)
	if e != nil || st.Uid != 0 || st.Nlink != 1 || st.Mode&unix.S_IFMT != unix.S_IFREG || (uint32(st.Mode)&0777 != mode && st.Mode&0777 != 0600) || st.Size < 0 || st.Size > int64(len(expected)) || !darwin.NoACL(fd) {
		unix.Close(fd)
		return -1, st, uncertain
	}
	data := make([]byte, st.Size)
	offset := 0
	for offset < len(data) {
		n, e := unix.Read(fd, data[offset:])
		if e == unix.EINTR {
			continue
		}
		if e != nil || n <= 0 {
			unix.Close(fd)
			return -1, st, uncertain
		}
		offset += n
	}
	if !bytes.Equal(data, expected[:len(data)]) || !namedCurrent(parent, name, st) || unix.Fchmod(fd, mode) != nil {
		unix.Close(fd)
		return -1, st, uncertain
	}
	return fd, st, nil
}
