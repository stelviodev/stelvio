//go:build darwin

package helper

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"github.com/stelviodev/traforo/darwin"
	"golang.org/x/sys/unix"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

const installationAttribute = "dev.stelvio.traforo.installation"
const pendingDaemon = darwin.State + "/daemon.pending"

var launchPlist = []byte(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>dev.stelvio.tunnel</string><key>ProgramArguments</key><array><string>/Library/PrivilegedHelperTools/stelvio-traforo</string><string>helper</string><string>serve</string></array><key>UserName</key><string>root</string><key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/bin:/bin</string></dict><key>Sockets</key><dict><key>Control</key><dict><key>SockPathName</key><string>/Library/Application Support/Stelvio/tunnel/helper.sock</string><key>SockPathMode</key><integer>438</integer><key>SockType</key><string>stream</string></dict></dict></dict></plist>
`)

type identity struct{ Device, Inode uint64 }

func identityOf(st unix.Stat_t) identity        { return identity{uint64(st.Dev), st.Ino} }
func (id identity) matches(st unix.Stat_t) bool { return id.Inode != 0 && id == identityOf(st) }

type installationReceipt struct {
	Image, Directory, Daemon, Socket identity
	Staging                          string
}

func (r installationReceipt) encode() ([]byte, error) {
	if len(r.Staging) > 63 {
		return nil, uncertain
	}
	buf := make([]byte, 136)
	copy(buf, "STLVINS1")
	pos := 8
	for _, v := range []identity{r.Image, r.Directory, r.Daemon, r.Socket} {
		binary.LittleEndian.PutUint64(buf[pos:], v.Device)
		binary.LittleEndian.PutUint64(buf[pos+8:], v.Inode)
		pos += 16
	}
	copy(buf[72:], r.Staging)
	return buf, nil
}
func readInstallation(fd int) (installationReceipt, bool, error) {
	var r installationReceipt
	buf := make([]byte, 137)
	n, e := unix.Fgetxattr(fd, installationAttribute, buf)
	if e == unix.ENOATTR {
		return r, false, nil
	}
	if e != nil || n != 136 || string(buf[:8]) != "STLVINS1" {
		return r, false, uncertain
	}
	ids := []*identity{&r.Image, &r.Directory, &r.Daemon, &r.Socket}
	for i, id := range ids {
		id.Device = binary.LittleEndian.Uint64(buf[8+16*i:])
		id.Inode = binary.LittleEndian.Uint64(buf[16+16*i:])
	}
	index := bytes.IndexByte(buf[72:136], 0)
	if index < 0 {
		return r, false, uncertain
	}
	r.Staging = string(buf[72 : 72+index])
	if r.Staging != "" {
		if !strings.HasPrefix(r.Staging, ".tunnel.") {
			return r, false, uncertain
		}
		for _, v := range r.Staging[8:] {
			if !(v >= 'a' && v <= 'z' || v >= 'A' && v <= 'Z' || v >= '0' && v <= '9') {
				return r, false, uncertain
			}
		}
	}
	st, e := stat(fd)
	if e != nil || !r.Image.matches(st) {
		return r, false, uncertain
	}
	return r, true, nil
}
func writeInstallation(fd int, r installationReceipt) error {
	if !trusted() {
		return uncertain
	}
	buf, e := r.encode()
	if e != nil {
		return e
	}
	if e = unix.Fsetxattr(fd, installationAttribute, buf, 0); e != nil {
		return e
	}
	return unix.Fsync(fd)
}
func launch(operation string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/bin/launchctl", operation, "system", darwin.Plist)
	cmd.Env = []string{"PATH=/usr/bin:/bin"}
	e := cmd.Run()
	if operation == "bootstrap" {
		if exit, ok := e.(*exec.ExitError); ok && exit.ExitCode() == 5 {
			return nil
		}
	}
	return e
}
func named(path string) (unix.Stat_t, error) {
	var st unix.Stat_t
	e := unix.Lstat(path, &st)
	return st, e
}
func exactFile(path string, id identity, expected []byte) bool {
	p, e := safeDirectory(filepath.Dir(path))
	if e != nil {
		return false
	}
	defer unix.Close(p)
	data, st, e := readAt(p, filepath.Base(path), len(expected))
	return e == nil && id.matches(st) && st.Nlink <= 2 && bytes.Equal(data, expected)
}
func prepare(fd int, r *installationReceipt) error {
	for _, path := range []string{"/Library/Application Support", "/Library/LaunchDaemons"} {
		p, e := safeDirectory(path)
		if e != nil {
			return e
		}
		unix.Close(p)
	}
	e := unix.Mkdir(darwin.StateParent, 0755)
	if e == nil {
		if e = unix.Chmod(darwin.StateParent, 0755); e != nil {
			return e
		}
		if e = syncDirectory("/Library/Application Support"); e != nil {
			return e
		}
	} else if e != unix.EEXIST {
		return e
	}
	p, e := safeDirectory(darwin.StateParent)
	if e != nil {
		return e
	}
	defer unix.Close(p)
	if r.Directory.Inode == 0 {
		if r.Staging == "" {
			if _, e := named(darwin.State); e != unix.ENOENT {
				return uncertain
			}
			var nonce [8]byte
			if _, e = rand.Read(nonce[:]); e != nil {
				return e
			}
			r.Staging = fmt.Sprintf(".tunnel.%x", nonce)
			if e = writeInstallation(fd, *r); e != nil {
				return e
			}
		}
		path := darwin.StateParent + "/" + r.Staging
		e = unix.Mkdir(path, 0711)
		if e == nil {
			if e = unix.Chmod(path, 0711); e != nil {
				return e
			}
		} else if e != unix.EEXIST {
			return e
		}
		stage, e := safeDirectory(path)
		if e != nil {
			return e
		}
		file := os.NewFile(uintptr(stage), path)
		entries, e := file.Readdirnames(1)
		st, se := stat(stage)
		ce := file.Close()
		if e != io.EOF || len(entries) != 0 || se != nil || ce != nil || st.Mode&0777 != 0711 {
			return uncertain
		}
		r.Directory = identityOf(st)
		if e = writeInstallation(fd, *r); e != nil {
			return e
		}
		if e = unix.Fsync(p); e != nil {
			return e
		}
	}
	if r.Staging != "" {
		source := darwin.StateParent + "/" + r.Staging
		if st, e := named(source); e == nil {
			if !r.Directory.matches(st) {
				return uncertain
			}
			if e = moveExclusive(source, darwin.State); e != nil {
				return e
			}
		} else if e != unix.ENOENT {
			return e
		}
		if e = unix.Fsync(p); e != nil {
			return e
		}
		st, e := named(darwin.State)
		if e != nil || !r.Directory.matches(st) {
			return uncertain
		}
		q, e := safeDirectory(darwin.State)
		if e != nil {
			return e
		}
		unix.Close(q)
		r.Staging = ""
		if e = writeInstallation(fd, *r); e != nil {
			return e
		}
	}
	st, e := named(darwin.State)
	if e != nil || !r.Directory.matches(st) {
		return uncertain
	}
	q, e := safeDirectory(darwin.State)
	if e != nil {
		return e
	}
	defer unix.Close(q)
	gate, e := unix.Openat(q, "admission", unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if e == nil {
		if e = unix.Fsync(gate); e != nil {
			unix.Close(gate)
			return e
		}
		if e = unix.Close(gate); e != nil {
			return e
		}
	} else if e != unix.EEXIST {
		return e
	}
	return unix.Fsync(q)
}
func publish(fd int, r *installationReceipt) error {
	p, e := safeDirectory(darwin.State)
	if e != nil {
		return e
	}
	defer unix.Close(p)
	if r.Daemon.Inode == 0 {
		stage, st, e := prefixAt(p, "daemon.pending", launchPlist, 0644)
		if e != nil {
			return e
		}
		e = writeAll(stage, launchPlist[int(st.Size):])
		if e == nil {
			e = unix.Fsync(stage)
		}
		c := unix.Close(stage)
		if e == nil {
			e = c
		}
		if e != nil {
			return e
		}
		r.Daemon = identityOf(st)
		if e = writeInstallation(fd, *r); e != nil {
			return e
		}
		if e = unix.Fsync(p); e != nil {
			return e
		}
	}
	if _, e = named(pendingDaemon); e == nil {
		if !exactFile(pendingDaemon, r.Daemon, launchPlist) {
			return uncertain
		}
		e = unix.Link(pendingDaemon, darwin.Plist)
		if e != nil && e != unix.EEXIST {
			return e
		}
		if !exactFile(darwin.Plist, r.Daemon, launchPlist) {
			return uncertain
		}
		if e = syncDirectory("/Library/LaunchDaemons"); e != nil {
			return e
		}
		if e = unix.Unlinkat(p, "daemon.pending", 0); e != nil {
			return e
		}
		if e = unix.Fsync(p); e != nil {
			return e
		}
	} else if e != unix.ENOENT {
		return e
	}
	if !exactFile(darwin.Plist, r.Daemon, launchPlist) {
		return uncertain
	}
	return nil
}
func recordSocket(fd int, r *installationReceipt) error {
	for i := 0; i < 50; i++ {
		st, e := named(darwin.Socket)
		if e == nil {
			if st.Mode&unix.S_IFMT != unix.S_IFSOCK || st.Uid != 0 || st.Mode&0777 != 0666 || !noACLPath(darwin.Socket) || r.Socket.Inode != 0 && !r.Socket.matches(st) {
				return uncertain
			}
			r.Socket = identityOf(st)
			if e = writeInstallation(fd, *r); e != nil {
				return e
			}
			return syncDirectory(darwin.State)
		}
		if e != unix.ENOENT {
			return e
		}
		time.Sleep(100 * time.Millisecond)
	}
	return uncertain
}

// Move shared entries into an exclusive namespace before verifying and deleting.
// If a concurrent writer replaced the entry, restore it exclusively or retain it.
func dispose(path, retired string, id identity, kind uint16, expected []byte) error {
	check := func(name string) bool {
		st, e := named(name)
		if e != nil || !id.matches(st) || st.Mode&unix.S_IFMT != kind || st.Uid != 0 || !noACLPath(name) {
			return false
		}
		if expected != nil {
			return exactFile(name, id, expected)
		}
		return true
	}
	if _, e := named(retired); e == unix.ENOENT {
		if _, e = named(path); e == unix.ENOENT {
			return syncDirectory(filepath.Dir(path))
		} else if e != nil {
			return e
		}
		if !check(path) {
			return uncertain
		}
		if e = moveExclusive(path, retired); e != nil {
			return e
		}
	} else if e != nil {
		return e
	}
	if !check(retired) {
		moveExclusive(retired, path)
		return uncertain
	}
	if e := unix.Unlink(retired); e != nil {
		return e
	}
	return syncDirectory(filepath.Dir(retired))
}
func emptyPrivate(name string) error {
	p, e := safeDirectory(darwin.State)
	if e != nil {
		return e
	}
	defer unix.Close(p)
	data, st, e := readAt(p, name, 0)
	if e == unix.ENOENT {
		return nil
	}
	if e != nil || len(data) != 0 || st.Nlink != 1 || st.Mode&0777 != 0600 {
		return uncertain
	}
	if e = unix.Unlinkat(p, name, 0); e != nil {
		return e
	}
	return unix.Fsync(p)
}
func uninstall(fd int, r *installationReceipt, gateFD int) error {
	st, e := named(darwin.State)
	absent := e == unix.ENOENT
	if e != nil && !absent {
		return e
	}
	if !absent {
		if !r.Directory.matches(st) {
			return uncertain
		}
		p, e := safeDirectory(darwin.State)
		if e != nil {
			return e
		}
		unix.Close(p)
		if gateFD < 0 {
			return uncertain
		}
	}
	if _, e := named(darwin.Plist); e == nil {
		if !exactFile(darwin.Plist, r.Daemon, launchPlist) {
			return uncertain
		}
	} else if e != unix.ENOENT {
		return e
	}
	launch("bootout")
	image := -1
	for i := 0; i < 350; i++ {
		image, e = lockFile(darwin.Installed, false, false, false)
		if e == nil {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if image < 0 {
		return uncertain
	}
	defer unix.Close(image)
	if !absent {
		lease, e := lockFile(darwin.State+"/lease", true, false, true)
		if e != nil {
			return e
		}
		store := Store{image, lease}
		state, exists, e := store.Load()
		if e == nil && exists && darwin.Alive(state.Peer) {
			e = uncertain
		}
		if e == nil {
			devices := initDevices()
			e = cleanup(store, &state, &devices)
		}
		unix.Close(lease)
		if e != nil {
			return e
		}
	}
	if r.Daemon.Inode != 0 {
		if e = dispose(darwin.Plist, "/Library/LaunchDaemons/.dev.stelvio.tunnel.retired", r.Daemon, unix.S_IFREG, launchPlist); e != nil {
			return e
		}
	}
	if !absent {
		if _, e := named(pendingDaemon); e == nil && r.Daemon.Inode == 0 {
			p, e := safeDirectory(darwin.State)
			if e != nil {
				return e
			}
			stage, st, e := prefixAt(p, "daemon.pending", launchPlist, 0644)
			if e == nil {
				e = writeAll(stage, launchPlist[int(st.Size):])
				if e == nil {
					e = unix.Fsync(stage)
				}
				unix.Close(stage)
			}
			unix.Close(p)
			if e != nil {
				return e
			}
			r.Daemon = identityOf(st)
			if e = writeInstallation(fd, *r); e != nil {
				return e
			}
		} else if e != nil && e != unix.ENOENT {
			return e
		}
		if r.Daemon.Inode != 0 {
			if e = dispose(pendingDaemon, darwin.State+"/daemon.retired", r.Daemon, unix.S_IFREG, launchPlist); e != nil {
				return e
			}
		}
		if e = dispose(darwin.Socket, darwin.State+"/helper.retired", r.Socket, unix.S_IFSOCK, nil); e != nil {
			return e
		}
		if e = emptyPrivate("lease"); e != nil {
			return e
		}
		if e = emptyPrivate("admission"); e != nil {
			return e
		}
		r.Staging = ".tunnel.retired"
		if e = writeInstallation(fd, *r); e != nil {
			return e
		}
		retired := darwin.StateParent + "/.tunnel.retired"
		if e = moveExclusive(darwin.State, retired); e != nil {
			return e
		}
		st, e = named(retired)
		if e != nil || !r.Directory.matches(st) {
			moveExclusive(retired, darwin.State)
			return uncertain
		}
		entries, e := os.ReadDir(retired)
		if e != nil || len(entries) != 0 {
			return uncertain
		}
		if e = unix.Rmdir(retired); e != nil {
			return e
		}
		if e = syncDirectory(darwin.StateParent); e != nil {
			return e
		}
	}
	return dispose(darwin.Installed, "/Library/PrivilegedHelperTools/.stelvio-traforo.retired", r.Image, unix.S_IFREG, nil)
}
func admin(install bool) error {
	if ok, stage := trustStage(); !ok {
		return fmt.Errorf("trusted image boundary %d", stage)
	}
	if install && darwin.Supported() != nil {
		return fmt.Errorf("unsupported Darwin capabilities")
	}
	unix.Umask(077)
	fd, e := unix.Open(darwin.Installed, unix.O_RDONLY|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0)
	if e != nil {
		return e
	}
	defer unix.Close(fd)
	r, exists, e := readInstallation(fd)
	if e != nil {
		return e
	}
	if !install {
		if !exists {
			image, e := lockFile(darwin.Installed, false, false, false)
			if e != nil {
				return e
			}
			defer unix.Close(image)
			_, currentExists, receiptError := readInstallation(fd)
			if receiptError != nil || currentExists {
				return uncertain
			}
			for _, path := range []string{darwin.State, darwin.Plist} {
				if _, e := named(path); e != unix.ENOENT {
					return uncertain
				}
			}
			st, e := stat(fd)
			if e != nil {
				return e
			}
			return dispose(darwin.Installed, "/Library/PrivilegedHelperTools/.stelvio-traforo.retired", identityOf(st), unix.S_IFREG, nil)
		}
		// The durable terminal intent allows retry after the directory disappeared.
		if r.Staging == ".tunnel.retired" {
			if _, pathError := named(darwin.State); pathError == unix.ENOENT {
				image, lockError := lockFile(darwin.Installed, false, false, false)
				if lockError != nil {
					return lockError
				}
				defer unix.Close(image)
				current, present, receiptError := readInstallation(fd)
				if receiptError != nil || !present || current.Staging != ".tunnel.retired" {
					return uncertain
				}
				r = current
				if _, pathError := named(darwin.Plist); pathError != unix.ENOENT {
					return uncertain
				}
				retired := darwin.StateParent + "/.tunnel.retired"
				if st, pathError := named(retired); pathError == nil {
					if !r.Directory.matches(st) {
						return uncertain
					}
					directory, directoryError := safeDirectory(retired)
					if directoryError != nil {
						return directoryError
					}
					file := os.NewFile(uintptr(directory), retired)
					names, readError := file.Readdirnames(1)
					file.Close()
					if readError != io.EOF || len(names) != 0 {
						return uncertain
					}
					if removeError := unix.Rmdir(retired); removeError != nil {
						return removeError
					}
				} else if pathError != unix.ENOENT {
					return pathError
				}
				if syncError := syncDirectory(darwin.StateParent); syncError != nil {
					return syncError
				}
				return dispose(darwin.Installed, "/Library/PrivilegedHelperTools/.stelvio-traforo.retired", r.Image, unix.S_IFREG, nil)
			}
		}
		g, gateError := gate(false)
		if gateError != nil {
			if _, pathError := named(darwin.State + "/admission"); pathError != unix.ENOENT {
				return fmt.Errorf("helper is active or admission is uncertain; close its session and retry")
			}
			image, lockError := lockFile(darwin.Installed, false, false, false)
			if lockError != nil {
				return lockError
			}
			current, present, receiptError := readInstallation(fd)
			if receiptError != nil || !present {
				unix.Close(image)
				return uncertain
			}
			r = current
			prepareError := prepare(fd, &r)
			unix.Close(image)
			if prepareError != nil {
				return prepareError
			}
			g, gateError = gate(false)
			if gateError != nil {
				return gateError
			}
		}
		defer unix.Close(g)
		r, exists, e = readInstallation(fd)
		if e != nil || !exists {
			return uncertain
		}
		return uninstall(fd, &r, g)
	}
	image, e := lockFile(darwin.Installed, false, false, false)
	if e != nil {
		return e
	}
	r, exists, e = readInstallation(fd)
	if e != nil {
		unix.Close(image)
		return e
	}
	g := -1
	defer func() {
		if image >= 0 {
			unix.Close(image)
		}
		if g >= 0 {
			unix.Close(g)
		}
	}()
	if _, e := named(darwin.State + "/admission"); e == nil {
		g, e = gate(false)
		if e != nil {
			return e
		}
	} else if e != unix.ENOENT {
		return e
	}
	if !exists {
		st, e := stat(fd)
		if e != nil {
			return e
		}
		r.Image = identityOf(st)
		if e = writeInstallation(fd, r); e != nil {
			return e
		}
		if e = syncDirectory("/Library/PrivilegedHelperTools"); e != nil {
			return e
		}
	}
	if e = prepare(fd, &r); e != nil {
		return e
	}
	if g < 0 {
		g, e = gate(false)
		if e != nil {
			return e
		}
	}
	if e = publish(fd, &r); e != nil {
		return e
	}
	unix.Close(image)
	image = -1
	if e = launch("bootstrap"); e != nil {
		return e
	}
	return recordSocket(fd, &r)
}
func Run(operation string) error {
	switch operation {
	case "serve":
		return serve()
	case "install":
		return admin(true)
	case "uninstall":
		return admin(false)
	default:
		return fmt.Errorf("unknown helper operation")
	}
}
