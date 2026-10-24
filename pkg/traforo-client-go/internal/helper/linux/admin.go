//go:build linux

package helper

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"time"

	"golang.org/x/sys/unix"
)

var unitFile = []byte(`[Unit]
Description=Stelvio Traforo privileged networking helper
After=dbus.service systemd-resolved.service
Wants=systemd-resolved.service

[Service]
Type=simple
ExecStart=/usr/local/libexec/stelvio-traforo helper serve
User=root
Group=root
Restart=on-failure
RestartSec=1
TimeoutStopSec=30
KillMode=control-group
UMask=0077
RuntimeDirectory=stelvio/tunnel
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=restart
StateDirectory=stelvio/tunnel
StateDirectoryMode=0755
NoNewPrivileges=yes
ProtectHome=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/stelvio/tunnel /run/stelvio/tunnel
RestrictAddressFamilies=AF_UNIX AF_NETLINK AF_INET AF_INET6

[Install]
WantedBy=multi-user.target
`)

const receiptPath = State + "/installation.json"

type installation struct {
	Version       int    `json:"version"`
	Digest        string `json:"digest"`
	Device        uint64 `json:"device"`
	Inode         uint64 `json:"inode"`
	ServiceDigest string `json:"service_sha256"`
}

func digest(path string) (string, error) {
	if e := securePath(path, false); e != nil {
		return "", e
	}
	f, e := os.Open(path)
	if e != nil {
		return "", e
	}
	defer func() { _ = f.Close() }()
	h := sha256.New()
	n, e := io.Copy(h, io.LimitReader(f, 16*1024*1024+1))
	if e != nil || n <= 0 || n > 16*1024*1024 {
		return "", errUncertain
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}
func imageReceipt() (installation, error) {
	d, e := digest(Installed)
	if e != nil {
		return installation{}, e
	}
	var st unix.Stat_t
	if e = unix.Lstat(Installed, &st); e != nil {
		return installation{}, e
	}
	serviceDigest := sha256.Sum256(unitFile)
	return installation{1, d, uint64(st.Dev), st.Ino, hex.EncodeToString(serviceDigest[:])}, nil
}
func installedReceipt() (installation, error) {
	var r installation
	if e := securePath(receiptPath, false); e != nil {
		return r, e
	}
	data, e := os.ReadFile(receiptPath)
	if e != nil {
		return r, e
	}
	if len(data) > 1024 || json.Unmarshal(data, &r) != nil || r.Version != 1 || len(r.Digest) != 64 || r.Device == 0 || r.Inode == 0 {
		return r, errUncertain
	}
	return r, nil
}
func verifyInstallation() error {
	r, e := installedReceipt()
	if e != nil {
		return e
	}
	current, e := imageReceipt()
	if e != nil || current != r {
		return errUncertain
	}
	if e = securePath(servicePath, false); e != nil {
		return e
	}
	data, e := os.ReadFile(servicePath)
	if e != nil || !bytes.Equal(data, unitFile) {
		return errUncertain
	}
	return nil
}
func systemctl(args ...string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 35*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/usr/bin/systemctl", args...)
	cmd.Env = []string{"PATH=/usr/bin:/bin", "LC_ALL=C"}
	output, e := cmd.CombinedOutput()
	if e != nil {
		return fmt.Errorf("systemctl %v: %w: %.4096s", args, e, output)
	}
	return nil
}
func install() error {
	exe, e := os.Executable()
	if e != nil || exe != Installed {
		return errors.New("install requires the checked system helper image")
	}
	lock, e := exclusive(State + "/admin.lock")
	if e != nil {
		return e
	}
	defer func() { _ = unix.Close(lock) }()
	if _, exists, e := load(); e != nil || exists {
		return errUncertain
	}
	if _, e = os.Lstat(servicePath); e == nil {
		if e = securePath(servicePath, false); e != nil {
			return e
		}
		old, e := os.ReadFile(servicePath)
		if e != nil {
			return e
		}
		receipt, err := installedReceipt()
		if err != nil {
			return err
		}
		sum := sha256.Sum256(old)
		legacy := bytes.Replace(unitFile, []byte("RuntimeDirectory=stelvio/tunnel\nRuntimeDirectoryMode=0755\nRuntimeDirectoryPreserve=restart\nStateDirectory=stelvio/tunnel\nStateDirectoryMode=0755\n"), nil, 1)
		if receipt.ServiceDigest != "" {
			if hex.EncodeToString(sum[:]) != receipt.ServiceDigest {
				return errUncertain
			}
		} else if !bytes.Equal(old, legacy) {
			return errUncertain
		}
		previous := Installed
		if _, err = os.Lstat("/usr/local/libexec/.stelvio-traforo.retired"); err == nil {
			previous = "/usr/local/libexec/.stelvio-traforo.retired"
		}
		previousDigest, err := digest(previous)
		if err != nil || previousDigest != receipt.Digest {
			return errUncertain
		}

	} else if !errors.Is(e, os.ErrNotExist) {
		return e
	}
	if e = atomicFile(servicePath, unitFile, 0644); e != nil {
		return e
	}
	r, e := imageReceipt()
	if e != nil {
		return e
	}
	data, e := json.Marshal(r)
	if e != nil {
		return e
	}
	if e = atomicFile(receiptPath, data, 0600); e != nil {
		return e
	}
	if e = systemctl("daemon-reload"); e != nil {
		return e
	}
	return systemctl("enable", "--now", "stelvio-traforo.service")
}
func uninstall() error {
	if e := verifyInstallation(); e != nil {
		return e
	}
	lock, e := exclusive(State + "/admin.lock")
	if e != nil {
		return e
	}
	defer func() { _ = unix.Close(lock) }()
	if _, exists, e := load(); e != nil || exists {
		return errUncertain
	}
	if e = systemctl("disable", "--now", "stelvio-traforo.service"); e != nil {
		return e
	}
	if _, exists, e := load(); e != nil || exists {
		return errUncertain
	}
	if e = verifyInstallation(); e != nil {
		return e
	}
	for _, p := range []string{servicePath, receiptPath, Installed} {
		if e = os.Remove(p); e != nil {
			return e
		}
		if e = syncDirectory(filepath.Dir(p)); e != nil {
			return e
		}
	}
	return systemctl("daemon-reload")
}

// Run never installs or mutates networking during package initialization.
func Run(operation string) error {
	if operation != "serve" && operation != "install" && operation != "uninstall" {
		return errors.New("unknown helper operation")
	}

	if os.Getuid() != 0 || os.Geteuid() != 0 {
		return errors.New("linux helper requires root")
	}
	if e := directories(); e != nil {
		return e
	}
	switch operation {
	case "serve":
		if e := verifyInstallation(); e != nil {
			return e
		}
		return serve()
	case "install":
		return install()
	case "uninstall":
		return uninstall()
	default:
		return errors.New("unknown helper operation")
	}
}
