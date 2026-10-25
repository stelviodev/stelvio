//go:build linux

// A native acceptance fixture: validate a real ELF image, then fail installation
// after replacement so the system bootstrap must restore the prior transaction.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"syscall"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "version" {
		fmt.Println("v0.0.7")
		return
	}
	// Simulate a failure after a new receipt and unit have been persisted.
	// The fixture is built only for explicitly selected privileged acceptance.
	if len(os.Args) == 3 && os.Args[1] == "helper" && os.Args[2] == "install" && os.Geteuid() == 0 {
		image := "/usr/local/libexec/stelvio-traforo"
		data, e := os.ReadFile(image)
		if e != nil {
			panic(e)
		}
		var st syscall.Stat_t
		if e = syscall.Stat(image, &st); e != nil {
			panic(e)
		}
		unit := []byte("[Unit]\nDescription=Traforo failed-install fixture\n[Service]\nType=oneshot\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n")
		digest, serviceDigest := sha256.Sum256(data), sha256.Sum256(unit)
		receipt, e := json.Marshal(map[string]any{"version": 1, "digest": hex.EncodeToString(digest[:]), "device": uint64(st.Dev), "inode": st.Ino, "service_sha256": hex.EncodeToString(serviceDigest[:])})
		if e != nil {
			panic(e)
		}
		if e = os.WriteFile("/var/lib/stelvio/tunnel/installation.json", receipt, 0600); e != nil {
			panic(e)
		}
		if e = os.WriteFile("/etc/systemd/system/stelvio-traforo.service", unit, 0644); e != nil {
			panic(e)
		}
	}
	os.Exit(19)
}
