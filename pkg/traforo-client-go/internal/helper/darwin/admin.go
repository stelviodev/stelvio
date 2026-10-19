//go:build darwin

package helper

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/stelviodev/traforo/internal/darwin"
	"golang.org/x/sys/unix"
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

func validStaging(value string) bool {
	if value == "" {
		return true
	}
	if len(value) <= len(".tunnel.") || len(value) > 63 || !strings.HasPrefix(value, ".tunnel.") {
		return false
	}
	for _, v := range value[len(".tunnel."):] {
		if (v < 'a' || v > 'z') && (v < 'A' || v > 'Z') && (v < '0' || v > '9') {
			return false
		}
	}
	return true
}

func (r installationReceipt) valid() bool {
	if r.Image.Inode == 0 || !validStaging(r.Staging) {
		return false
	}
	for _, id := range []identity{r.Directory, r.Daemon, r.Socket} {
		if id.Inode == 0 && id.Device != 0 {
			return false
		}
	}
	return true
}

func (r installationReceipt) encode() ([]byte, error) {
	if !r.valid() {
		return nil, errUncertain
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
func decodeInstallation(buf []byte) (installationReceipt, error) {
	var r installationReceipt
	if len(buf) != 136 || string(buf[:8]) != "STLVINS1" {
		return r, errUncertain
	}
	ids := []*identity{&r.Image, &r.Directory, &r.Daemon, &r.Socket}
	for i, id := range ids {
		id.Device = binary.LittleEndian.Uint64(buf[8+16*i:])
		id.Inode = binary.LittleEndian.Uint64(buf[16+16*i:])
	}
	index := bytes.IndexByte(buf[72:136], 0)
	if index < 0 {
		return r, errUncertain
	}
	r.Staging = string(buf[72 : 72+index])
	for _, value := range buf[72+index+1 : 136] {
		if value != 0 {
			return r, errUncertain
		}
	}
	if !r.valid() {
		return r, errUncertain
	}
	return r, nil
}

func readInstallation(fd int) (installationReceipt, bool, error) {
	var empty installationReceipt
	buf := make([]byte, 137)
	n, e := unix.Fgetxattr(fd, installationAttribute, buf)
	if e == unix.ENOATTR {
		return empty, false, nil
	}
	if e != nil || n != 136 {
		return empty, false, errUncertain
	}
	r, e := decodeInstallation(buf[:n])
	if e != nil {
		return empty, false, e
	}
	st, e := stat(fd)
	if e != nil || !r.Image.matches(st) {
		return empty, false, errUncertain
	}
	return r, true, nil
}
func writeInstallation(fd int, r installationReceipt) error {
	if !trusted() {
		return errUncertain
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
	defer func() { _ = unix.Close(p) }() // Close the directory after inspection.
	data, st, e := readAt(p, filepath.Base(path), len(expected))
	return e == nil && id.matches(st) && st.Nlink <= 2 && bytes.Equal(data, expected)
}

type adminRecoveryAction uint8

const (
	adminRecoveryUncertain adminRecoveryAction = iota
	adminRecoveryMove
	adminRecoveryRemove
	adminRecoveryComplete
)

func stagingRecovery(sourcePresent, sourceOwned, targetPresent, targetOwned bool) adminRecoveryAction {
	if sourcePresent {
		if !sourceOwned || targetPresent {
			return adminRecoveryUncertain
		}
		return adminRecoveryMove
	}
	if targetPresent && targetOwned {
		return adminRecoveryComplete
	}
	return adminRecoveryUncertain
}

func daemonRecovery(pendingPresent, pendingOwned, publicPresent, publicOwned bool) adminRecoveryAction {
	if publicPresent && !publicOwned || pendingPresent && !pendingOwned {
		return adminRecoveryUncertain
	}
	switch {
	case pendingPresent && publicPresent:
		return adminRecoveryRemove
	case pendingPresent:
		return adminRecoveryMove
	case publicPresent:
		return adminRecoveryComplete
	default:
		return adminRecoveryUncertain
	}
}

func retiredDirectoryRecovery(statePresent, stateOwned, retiredPresent, retiredOwned, retiredEmpty bool) adminRecoveryAction {
	if statePresent && !stateOwned || retiredPresent && (!retiredOwned || !retiredEmpty) {
		return adminRecoveryUncertain
	}
	switch {
	case statePresent && !retiredPresent:
		return adminRecoveryMove
	case !statePresent && retiredPresent:
		return adminRecoveryRemove
	case !statePresent && !retiredPresent:
		return adminRecoveryComplete
	default:
		return adminRecoveryUncertain
	}
}

func prepare(fd int, r *installationReceipt) error {
	for _, path := range []string{"/Library/Application Support", "/Library/LaunchDaemons"} {
		p, e := safeDirectory(path)
		if e != nil {
			return e
		}
		if e = unix.Close(p); e != nil {
			return e
		}
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
	defer func() { _ = unix.Close(p) }() // Close the directory on every return path.
	if r.Directory.Inode == 0 {
		if r.Staging == "" {
			if _, e := named(darwin.State); e != unix.ENOENT {
				return errUncertain
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
			return errUncertain
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
		sourceStat, sourceError := named(source)
		if sourceError != nil && sourceError != unix.ENOENT {
			return sourceError
		}
		targetStat, targetError := named(darwin.State)
		if targetError != nil && targetError != unix.ENOENT {
			return targetError
		}
		sourcePresent, targetPresent := sourceError == nil, targetError == nil
		action := stagingRecovery(
			sourcePresent,
			sourcePresent && r.Directory.matches(sourceStat),
			targetPresent,
			targetPresent && r.Directory.matches(targetStat),
		)
		if action == adminRecoveryMove {
			if e = moveExclusive(source, darwin.State); e != nil {
				return e
			}
		} else if action != adminRecoveryComplete {
			return errUncertain
		}
		if e = unix.Fsync(p); e != nil {
			return e
		}
		st, e := named(darwin.State)
		if e != nil || !r.Directory.matches(st) {
			return errUncertain
		}
		q, e := safeDirectory(darwin.State)
		if e != nil {
			return e
		}
		if e = unix.Close(q); e != nil {
			return e
		}
		r.Staging = ""
		if e = writeInstallation(fd, *r); e != nil {
			return e
		}
	}
	st, e := named(darwin.State)
	if e != nil || !r.Directory.matches(st) {
		return errUncertain
	}
	q, e := safeDirectory(darwin.State)
	if e != nil {
		return e
	}
	defer func() { _ = unix.Close(q) }() // Close the state-directory handle after publication.
	gate, e := unix.Openat(q, "admission", unix.O_WRONLY|unix.O_CREAT|unix.O_EXCL|unix.O_NOFOLLOW|unix.O_CLOEXEC, 0600)
	if e == nil {
		if e = unix.Fsync(gate); e != nil {
			return errors.Join(e, unix.Close(gate))
		}
		if e = unix.Close(gate); e != nil {
			return e
		}
	} else if e != unix.EEXIST {
		return e
	}
	return unix.Fsync(q)
}
func publish(fd int, r *installationReceipt) (returnErr error) {
	p, e := safeDirectory(darwin.State)
	if e != nil {
		return e
	}
	defer func() { returnErr = errors.Join(returnErr, unix.Close(p)) }()
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
	pendingStat, pendingError := named(pendingDaemon)
	if pendingError != nil && pendingError != unix.ENOENT {
		return pendingError
	}
	_, publicError := named(darwin.Plist)
	if publicError != nil && publicError != unix.ENOENT {
		return publicError
	}
	pendingPresent, publicPresent := pendingError == nil, publicError == nil
	action := daemonRecovery(
		pendingPresent,
		pendingPresent && exactFile(pendingDaemon, r.Daemon, launchPlist),
		publicPresent,
		publicPresent && exactFile(darwin.Plist, r.Daemon, launchPlist),
	)
	switch action {
	case adminRecoveryMove:
		e = unix.Link(pendingDaemon, darwin.Plist)
		if e != nil && e != unix.EEXIST {
			return e
		}
		if !exactFile(darwin.Plist, r.Daemon, launchPlist) {
			return errUncertain
		}
		if e = syncDirectory("/Library/LaunchDaemons"); e != nil {
			return e
		}
		e = unix.Unlinkat(p, "daemon.pending", 0)
	case adminRecoveryRemove:
		if !pendingStatMatches(p, pendingStat, r.Daemon, launchPlist) {
			return errUncertain
		}
		if e = syncDirectory("/Library/LaunchDaemons"); e != nil {
			return e
		}
		e = unix.Unlinkat(p, "daemon.pending", 0)
	case adminRecoveryComplete:
		if e = syncDirectory("/Library/LaunchDaemons"); e != nil {
			return e
		}
		if e = unix.Fsync(p); e != nil {
			return e
		}
		if !exactFile(darwin.Plist, r.Daemon, launchPlist) {
			return errUncertain
		}
		return nil
	default:
		return errUncertain
	}
	if e != nil {
		return e
	}
	if e = unix.Fsync(p); e != nil {
		return e
	}
	if !exactFile(darwin.Plist, r.Daemon, launchPlist) {
		return errUncertain
	}
	return nil
}

func pendingStatMatches(parent int, before unix.Stat_t, id identity, expected []byte) bool {
	data, after, err := readAt(parent, "daemon.pending", len(expected))
	return err == nil && id.matches(before) && id.matches(after) && bytes.Equal(data, expected)
}
func recordSocket(fd int, r *installationReceipt) error {
	for i := 0; i < 50; i++ {
		st, e := named(darwin.Socket)
		if e == nil {
			if st.Mode&unix.S_IFMT != unix.S_IFSOCK || st.Uid != 0 || st.Mode&0777 != 0666 || !noACLPath(darwin.Socket) || r.Socket.Inode != 0 && !r.Socket.matches(st) {
				return errUncertain
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
	return errUncertain
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
			return errUncertain
		}
		if e = moveExclusive(path, retired); e != nil {
			return e
		}
	} else if e != nil {
		return e
	}
	if !check(retired) {
		if restoreErr := moveExclusive(retired, path); restoreErr != nil {
			return errors.Join(errUncertain, fmt.Errorf("restore retired entry: %w", restoreErr))
		}
		return errUncertain
	}
	if e := unix.Unlink(retired); e != nil {
		return e
	}
	return syncDirectory(filepath.Dir(retired))
}
func emptyPrivate(name string) (returnErr error) {
	p, e := safeDirectory(darwin.State)
	if e != nil {
		return e
	}
	defer func() { returnErr = errors.Join(returnErr, unix.Close(p)) }()
	data, st, e := readAt(p, name, 0)
	if e == unix.ENOENT {
		return nil
	}
	if e != nil || len(data) != 0 || st.Nlink != 1 || st.Mode&0777 != 0600 {
		return errUncertain
	}
	if e = unix.Unlinkat(p, name, 0); e != nil {
		return e
	}
	return unix.Fsync(p)
}

func emptyDirectory(path string) (bool, error) {
	fd, err := safeDirectory(path)
	if err != nil {
		return false, err
	}
	file := os.NewFile(uintptr(fd), path)
	names, readError := file.Readdirnames(1)
	closeError := file.Close()
	if closeError != nil {
		return false, closeError
	}
	if errors.Is(readError, io.EOF) && len(names) == 0 {
		return true, nil
	}
	if readError != nil {
		return false, readError
	}
	return len(names) == 0, nil
}

func finishRetiredDirectory(r *installationReceipt) error {
	retired := darwin.StateParent + "/.tunnel.retired"
	for range 4 {
		stateStat, stateError := named(darwin.State)
		if stateError != nil && stateError != unix.ENOENT {
			return stateError
		}
		retiredStat, retiredError := named(retired)
		if retiredError != nil && retiredError != unix.ENOENT {
			return retiredError
		}
		statePresent, retiredPresent := stateError == nil, retiredError == nil
		retiredEmpty := false
		if retiredPresent && r.Directory.matches(retiredStat) {
			var err error
			retiredEmpty, err = emptyDirectory(retired)
			if err != nil {
				return err
			}
		}
		action := retiredDirectoryRecovery(
			statePresent,
			statePresent && r.Directory.matches(stateStat),
			retiredPresent,
			retiredPresent && r.Directory.matches(retiredStat),
			retiredEmpty,
		)
		switch action {
		case adminRecoveryMove:
			if err := moveExclusive(darwin.State, retired); err != nil {
				return err
			}
		case adminRecoveryRemove:
			if err := unix.Rmdir(retired); err != nil {
				return err
			}
			if err := syncDirectory(darwin.StateParent); err != nil {
				return err
			}
		case adminRecoveryComplete:
			return syncDirectory(darwin.StateParent)
		default:
			return errUncertain
		}
	}
	return errUncertain
}

func uninstall(fd int, r *installationReceipt, gateFD int) (returnErr error) {
	st, e := named(darwin.State)
	absent := e == unix.ENOENT
	if e != nil && !absent {
		return e
	}
	if !absent {
		if !r.Directory.matches(st) {
			return errUncertain
		}
		p, e := safeDirectory(darwin.State)
		if e != nil {
			return e
		}
		if e = unix.Close(p); e != nil {
			return e
		}
		if gateFD < 0 {
			return errUncertain
		}
	}
	if _, e := named(darwin.Plist); e == nil {
		if !exactFile(darwin.Plist, r.Daemon, launchPlist) {
			return errUncertain
		}
	} else if e != unix.ENOENT {
		return e
	}
	_ = launch("bootout") // The lock check below verifies that the service has exited.
	image := -1
	for i := 0; i < 350; i++ {
		image, e = lockFile(darwin.Installed, false, false, false)
		if e == nil {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if image < 0 {
		return errUncertain
	}
	defer func() { returnErr = errors.Join(returnErr, unix.Close(image)) }()
	if !absent {
		lease, e := lockFile(darwin.State+"/lease", true, false, true)
		if e != nil {
			return e
		}
		store := Store{image, lease}
		state, exists, e := store.Load()
		if e == nil && exists && darwin.Alive(state.Peer) && (state.Parent.PID == 0 || darwin.ParentAlive(state.Parent)) {
			e = errUncertain
		}
		if e == nil {
			devices := initDevices()
			e = cleanup(store, &state, &devices)
		}
		e = errors.Join(e, unix.Close(lease))
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
				e = errors.Join(e, unix.Close(stage))
			}
			e = errors.Join(e, unix.Close(p))
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
		if e = finishRetiredDirectory(r); e != nil {
			return e
		}
	}
	return dispose(darwin.Installed, "/Library/PrivilegedHelperTools/.stelvio-traforo.retired", r.Image, unix.S_IFREG, nil)
}
func admin(install bool) (returnErr error) {
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
	defer func() { returnErr = errors.Join(returnErr, unix.Close(fd)) }()
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
			defer func() { returnErr = errors.Join(returnErr, unix.Close(image)) }()
			_, currentExists, receiptError := readInstallation(fd)
			if receiptError != nil || currentExists {
				return errUncertain
			}
			for _, path := range []string{darwin.State, darwin.Plist} {
				if _, e := named(path); e != unix.ENOENT {
					return errUncertain
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
				defer func() { returnErr = errors.Join(returnErr, unix.Close(image)) }()
				current, present, receiptError := readInstallation(fd)
				if receiptError != nil || !present || current.Staging != ".tunnel.retired" {
					return errUncertain
				}
				r = current
				if _, pathError := named(darwin.Plist); pathError != unix.ENOENT {
					return errUncertain
				}
				if recoveryError := finishRetiredDirectory(&r); recoveryError != nil {
					return recoveryError
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
				return errors.Join(errUncertain, unix.Close(image))
			}
			r = current
			prepareError := prepare(fd, &r)
			prepareError = errors.Join(prepareError, unix.Close(image))
			if prepareError != nil {
				return prepareError
			}
			g, gateError = gate(false)
			if gateError != nil {
				return gateError
			}
		}
		defer func() { returnErr = errors.Join(returnErr, unix.Close(g)) }()
		r, exists, e = readInstallation(fd)
		if e != nil || !exists {
			return errUncertain
		}
		return uninstall(fd, &r, g)
	}
	image, e := lockFile(darwin.Installed, false, false, false)
	if e != nil {
		return e
	}
	r, exists, e = readInstallation(fd)
	if e != nil {
		return errors.Join(e, unix.Close(image))
	}
	g := -1
	defer func() {
		if image >= 0 {
			returnErr = errors.Join(returnErr, unix.Close(image))
		}
		if g >= 0 {
			returnErr = errors.Join(returnErr, unix.Close(g))
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
	closeErr := unix.Close(image)
	image = -1
	if closeErr != nil {
		return closeErr
	}
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
